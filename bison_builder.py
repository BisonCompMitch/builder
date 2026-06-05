#!/usr/bin/env python3
from __future__ import annotations

import base64
import datetime
import io
import math
import os
import re
import sys
import threading
import uuid
from array import array
from pathlib import Path

try:
    from reportlab.pdfgen import canvas as _rl_canvas
    from reportlab.lib.pagesizes import landscape as _rl_landscape, letter as _rl_letter
    from reportlab.lib import colors as _rl_colors
    _REPORTLAB = True
except ImportError:
    _REPORTLAB = False

from flask import Flask, jsonify, render_template, request, send_file

# Support running from BisonBuilder/ subfolder or directly from Patch1/ root
_HERE = Path(__file__).resolve().parent
for _search in (_HERE, _HERE.parent):
    if (_search / "convert_to_sdp.py").exists():
        if str(_search) not in sys.path:
            sys.path.insert(0, str(_search))
        break

try:
    from convert_to_sdp import (
        ConversionOptions,
        SUPPORTED_SOURCE_EXTS,
        convert_source_to_sdp,
        write_conversion_report,
        converter_capabilities,
    )
    _CONVERTER_AVAILABLE = True
except ImportError:
    _CONVERTER_AVAILABLE = False

try:
    import ifcopenshell
    import ifcopenshell.geom
except Exception:
    ifcopenshell = None

BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / "_runtime_builder"
UPLOAD_DIR = RUNTIME_DIR / "uploads"
OUTPUT_DIR = RUNTIME_DIR / "outputs"
REPORT_DIR = RUNTIME_DIR / "reports"

for _d in (UPLOAD_DIR, OUTPUT_DIR, REPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

app = Flask(
    __name__,
    static_folder=str(BASE_DIR / "static"),
    template_folder=str(BASE_DIR / "templates"),
)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

_downloads_lock = threading.Lock()
_download_registry: dict[str, dict[str, str]] = {}

COMPONENT_COLORS: dict[str, int] = {
    "Walls": 0x71B7FF,
    "Roofs": 0xF4B860,
    "Floors": 0x5CD6B8,
    "Ceilings": 0xC68CFF,
    "Trusses": 0xFF8F70,
    "Structure": 0x9AA6B2,
    "Other": 0x8EA0AF,
}


def _sanitize_filename(filename: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in filename).strip()
    return safe or "uploaded"


def _register_download(output_path: Path, report_path: Path) -> str:
    token = uuid.uuid4().hex
    with _downloads_lock:
        _download_registry[token] = {
            "output": str(output_path),
            "report": str(report_path),
        }
    return token


def _name_prefix(name: str | None) -> str | None:
    if not isinstance(name, str):
        return None
    m = re.match(r"[A-Za-z]+", name.strip())
    return m.group(0).upper() if m else None


def _component_bucket(entity: object) -> str:
    prefix = _name_prefix(getattr(entity, "Name", None))
    if prefix:
        if prefix.startswith("W"):
            return "Walls"
        if prefix.startswith("R"):
            return "Roofs"
        if prefix.startswith("C"):
            return "Ceilings"
        if prefix.startswith("F"):
            return "Floors"
        if prefix.startswith("T"):
            return "Trusses"

    etype = getattr(entity, "is_a", lambda: "Other")().upper()
    if "WALL" in etype:
        return "Walls"
    if "ROOF" in etype:
        return "Roofs"
    if "FLOOR" in etype or "SLAB" in etype:
        return "Floors"
    if "CEILING" in etype:
        return "Ceilings"
    if "TRUSS" in etype or "BEAM" in etype or "MEMBER" in etype:
        return "Structure"
    return "Other"


def _encode_f32(values: tuple) -> str:
    arr = array("f", (float(v) for v in values))
    return base64.b64encode(arr.tobytes()).decode("ascii")


def _encode_u32(values: tuple) -> str:
    arr = array("I", (int(v) for v in values))
    return base64.b64encode(arr.tobytes()).decode("ascii")


def _get_parent_assembly(entity: object) -> object | None:
    try:
        for rel in getattr(entity, "Decomposes", None) or ():
            parent = getattr(rel, "RelatingObject", None)
            if parent is not None:
                return parent
    except Exception:
        pass
    return None


def _build_builder_payload(ifc_path: Path) -> dict:
    if ifcopenshell is None:
        raise RuntimeError("IFC support unavailable — install ifcopenshell.")

    model = ifcopenshell.open(str(ifc_path))
    settings = ifcopenshell.geom.settings()
    try:
        settings.set("use-world-coords", True)
    except Exception:
        pass

    include = [e for e in model.by_type("IfcProduct") if getattr(e, "Representation", None)]
    if not include:
        raise ValueError("No IFC products with geometry found.")

    num_threads = max(1, min(4, os.cpu_count() or 1))
    iterator = ifcopenshell.geom.iterator(settings, model, num_threads, include=include)
    if not iterator.initialize():
        raise ValueError("IFC geometry iterator failed to initialize.")

    entities = []
    skipped = 0

    while True:
        shape = iterator.get()
        entity = model.by_id(shape.id)
        geom = getattr(shape, "geometry", None)
        verts = tuple(getattr(geom, "verts", ()) or ())
        faces = tuple(getattr(geom, "faces", ()) or ())

        if len(verts) < 3 or len(faces) < 3:
            skipped += 1
        else:
            parent = _get_parent_assembly(entity)
            if parent is not None:
                panel_id = parent.id()
                panel_global_id = getattr(parent, "GlobalId", None)
                panel_name = getattr(parent, "Name", None) or "Assembly"
                panel_type = parent.is_a()
                bucket = _component_bucket(parent)
            else:
                panel_id = shape.id
                panel_global_id = getattr(entity, "GlobalId", None)
                panel_name = getattr(entity, "Name", None) or "Unnamed"
                panel_type = entity.is_a()
                bucket = _component_bucket(entity)

            entities.append({
                "id": shape.id,
                "globalId": getattr(entity, "GlobalId", None),
                "name": getattr(entity, "Name", None) or "Unnamed",
                "type": entity.is_a(),
                "panelId": panel_id,
                "panelGlobalId": panel_global_id,
                "panelName": panel_name,
                "panelType": panel_type,
                "bucket": bucket,
                "color": COMPONENT_COLORS.get(bucket, COMPONENT_COLORS["Other"]),
                "positionsB64": _encode_f32(verts),
                "indicesB64": _encode_u32(faces),
            })

        if not iterator.next():
            break

    if not entities:
        raise ValueError("No renderable geometry found in this IFC file.")

    return {
        "kind": "builder",
        "sourceName": ifc_path.name,
        "entityCount": len(entities),
        "skipped": skipped,
        "entities": entities,
    }


def _generate_pdf(model: object, job_name: str) -> bytes:
    """Generate a multi-page construction PDF from an IFC model."""
    PW, PH = _rl_landscape(_rl_letter)   # 792 × 612 pts
    MARGIN  = 32
    HDR_H   = 58
    FTR_H   = 52
    DRAW_X  = MARGIN
    DRAW_Y  = MARGIN + FTR_H
    DRAW_W  = PW - 2 * MARGIN
    DRAW_H  = PH - 2 * MARGIN - HDR_H - FTR_H
    TODAY   = datetime.date.today().strftime("%d %b %Y")

    buf  = io.BytesIO()
    canv = _rl_canvas.Canvas(buf, pagesize=(PW, PH))
    page_num = [0]

    # ── page chrome ──────────────────────────────────────────────────────────
    def _new_page(title: str) -> None:
        if page_num[0] > 0:
            canv.showPage()
        page_num[0] += 1

        canv.setFillColor(_rl_colors.white)
        canv.rect(0, 0, PW, PH, fill=1, stroke=0)

        canv.setStrokeColor(_rl_colors.black)
        canv.setLineWidth(1.5)
        canv.rect(MARGIN, MARGIN, DRAW_W, PH - 2 * MARGIN, fill=0, stroke=1)

        hdr_y = PH - MARGIN - HDR_H
        canv.setLineWidth(0.75)
        canv.line(MARGIN, hdr_y, MARGIN + DRAW_W, hdr_y)

        logo_w = 64
        canv.setLineWidth(0.5)
        canv.line(MARGIN + logo_w, hdr_y, MARGIN + logo_w, PH - MARGIN)
        canv.setFillColor(_rl_colors.black)
        canv.setFont("Helvetica-Bold", 11)
        canv.drawCentredString(MARGIN + logo_w / 2, hdr_y + HDR_H * 0.58, "BISON")
        canv.setFont("Helvetica-Bold", 8)
        canv.drawCentredString(MARGIN + logo_w / 2, hdr_y + HDR_H * 0.28, "WORKS")

        mid_x = MARGIN + logo_w + (DRAW_W - logo_w) / 2
        canv.setFont("Helvetica-Bold", 14)
        canv.setFillColor(_rl_colors.black)
        canv.drawCentredString(mid_x, hdr_y + HDR_H * 0.60, title)
        canv.setFont("Helvetica", 9)
        canv.drawCentredString(mid_x, hdr_y + HDR_H * 0.28, job_name)

        canv.setLineWidth(0.75)
        canv.line(MARGIN, MARGIN + FTR_H, MARGIN + DRAW_W, MARGIN + FTR_H)

        info_w = 210
        div_x  = MARGIN + DRAW_W - info_w
        canv.line(div_x, MARGIN, div_x, MARGIN + FTR_H)

        row_h = FTR_H / 3
        for i in range(1, 3):
            canv.setLineWidth(0.4)
            canv.line(div_x, MARGIN + row_h * i, MARGIN + DRAW_W, MARGIN + row_h * i)

        val_x = div_x + 58
        canv.setLineWidth(0.4)
        canv.line(val_x, MARGIN, val_x, MARGIN + FTR_H)

        labels = ["Job Name", "Date", "Page"]
        values = [job_name[:42], TODAY, f"Page {page_num[0]}"]
        for i, (lbl, val) in enumerate(zip(labels, values)):
            cy = MARGIN + row_h * (2 - i) + row_h / 2 - 3
            canv.setFont("Helvetica", 6)
            canv.setFillColor(_rl_colors.black)
            canv.drawString(div_x + 3, cy, lbl)
            canv.setFont("Helvetica", 7)
            canv.drawString(val_x + 3, cy, val)

        canv.setFont("Helvetica", 6)
        canv.setFillColor(_rl_colors.HexColor("#555555"))
        canv.drawString(MARGIN + 4, MARGIN + 4, "BisonBuilder")

    BUCKET_RGB = {
        "Walls":     (0.0,  0.0,  0.0),
        "Trusses":   (0.0,  0.18, 0.50),
        "Floors":    (0.0,  0.35, 0.18),
        "Roofs":     (0.40, 0.15, 0.0),
        "Structure": (0.22, 0.08, 0.40),
        "Ceilings":  (0.30, 0.0,  0.30),
        "Other":     (0.38, 0.38, 0.38),
    }

    # ── Andrew's monotone-chain convex hull ───────────────────────────────────
    def _hull(pts: list) -> list:
        pts = sorted(set(pts))
        if len(pts) < 2:
            return pts
        def cross(O, A, B):
            return (A[0] - O[0]) * (B[1] - O[1]) - (A[1] - O[1]) * (B[0] - O[0])
        lower: list = []
        for p in pts:
            while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
                lower.pop()
            lower.append(p)
        upper: list = []
        for p in reversed(pts):
            while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
                upper.pop()
            upper.append(p)
        return lower[:-1] + upper[:-1]

    def _project(verts_flat: tuple, view: str) -> list:
        pts = []
        for i in range(0, len(verts_flat) - 2, 3):
            x, y, z = verts_flat[i], verts_flat[i + 1], verts_flat[i + 2]
            if view == "plan":
                pts.append((x, y))
            elif view == "north":
                pts.append((x, z))
            elif view == "south":
                pts.append((-x, z))
            elif view == "east":
                pts.append((-y, z))
            else:
                pts.append((y, z))
        return pts

    # ── wall-plan page renderer — bbox per panel + opening cutouts ───────────
    def _draw_wall_plan_page(title: str, wall_items: list) -> None:
        """wall_items: [{name, bucket, bbox:(x0,y0,x1,y1), op_bboxes:[(x0,y0,x1,y1)...]}]"""
        if not wall_items:
            return
        _new_page(title)

        ax0 = min(it["bbox"][0] for it in wall_items)
        ay0 = min(it["bbox"][1] for it in wall_items)
        ax1 = max(it["bbox"][2] for it in wall_items)
        ay1 = max(it["bbox"][3] for it in wall_items)
        rx = ax1 - ax0 or 1.0
        ry = ay1 - ay0 or 1.0

        pad         = 22
        lbl_reserve = 54
        aw    = DRAW_W - 2 * pad - lbl_reserve
        ah    = DRAW_H - 2 * pad
        scale = min(aw / rx, ah / ry)
        ox    = DRAW_X + pad + (aw - rx * scale) / 2
        oy    = DRAW_Y + pad + (ah - ry * scale) / 2

        def wpdf(x: float, y: float) -> tuple:
            return ox + (x - ax0) * scale, oy + (y - ay0) * scale

        for item in wall_items:
            r, g, b = BUCKET_RGB.get(item["bucket"], (0, 0, 0))
            bx0, by0, bx1, by1 = item["bbox"]
            px0, py0 = wpdf(bx0, by0)
            pw = (bx1 - bx0) * scale
            ph = (by1 - by0) * scale
            if pw < 0.4 or ph < 0.4:
                continue

            canv.setFillColorRGB(r * 0.06 + 0.94, g * 0.06 + 0.94, b * 0.06 + 0.94)
            canv.setStrokeColorRGB(r, g, b)
            canv.setLineWidth(0.6)
            canv.rect(px0, py0, pw, ph, fill=1, stroke=1)

            # Opening cutouts (doors/windows) — white rectangle with light border
            for ox0b, oy0b, ox1b, oy1b in item.get("op_bboxes", []):
                opx0, opy0 = wpdf(ox0b, oy0b)
                opw = (ox1b - ox0b) * scale
                oph = (oy1b - oy0b) * scale
                if opw > 0.5 and oph > 0.5:
                    canv.setFillColor(_rl_colors.white)
                    canv.setStrokeColorRGB(0.65, 0.65, 0.65)
                    canv.setLineWidth(0.35)
                    canv.rect(opx0, opy0, opw, oph, fill=1, stroke=1)

            # Per-panel dimension annotation (wall length in inches, ScotSteel style)
            wall_len_m = max(bx1 - bx0, by1 - by0)
            is_horiz   = (bx1 - bx0) >= (by1 - by0)
            if wall_len_m >= 0.30:
                dim_txt = _fmt_inches(wall_len_m)
                canv.setFillColorRGB(0.25, 0.25, 0.25)
                canv.setStrokeColorRGB(0.45, 0.45, 0.45)
                canv.setLineWidth(0.3)
                if is_horiz and pw >= 14:
                    ty = py0 + ph + 1.5
                    canv.line(px0,      py0 + ph, px0,      ty + 5)
                    canv.line(px0 + pw, py0 + ph, px0 + pw, ty + 5)
                    canv.line(px0 + 1,  ty + 3,   px0 + pw - 1, ty + 3)
                    canv.setFont("Helvetica", 4.5)
                    canv.drawCentredString(px0 + pw / 2, ty + 4.5, dim_txt)
                elif not is_horiz and ph >= 14:
                    canv.saveState()
                    canv.translate(px0 - 2, py0 + ph / 2)
                    canv.rotate(90)
                    canv.setFont("Helvetica", 4.5)
                    canv.drawCentredString(0, 1, dim_txt)
                    canv.restoreState()

            # Label (RED bold) to the right of the panel — clamped + halo
            if item["name"]:
                fs = 5.5
                label_text = item["name"][:20]
                lx = px0 + pw + 4
                ly = py0 + ph / 2
                tw = canv.stringWidth(label_text, "Helvetica-Bold", fs)
                right_limit = DRAW_X + DRAW_W - 4
                lx = min(lx, right_limit - tw)
                canv.setLineWidth(0.3)
                canv.setStrokeColorRGB(0.55, 0.55, 0.55)
                canv.line(px0 + pw, ly, lx - 1, ly)
                hp = 1.5
                canv.setFillColor(_rl_colors.white)
                canv.rect(lx - hp, ly - fs * 0.82, tw + hp * 2, fs + hp, fill=1, stroke=0)
                canv.setFont("Helvetica-Bold", fs)
                canv.setFillColorRGB(0.80, 0.02, 0.02)
                canv.drawString(lx, ly - fs * 0.36, label_text)

        mm_per_pt = 25.4 / 72
        ratio     = 1000 / (scale * mm_per_pt)
        canv.setFont("Helvetica", 6.5)
        canv.setFillColor(_rl_colors.black)
        canv.drawString(DRAW_X + 4, DRAW_Y + 4, f"Scale  1 : {ratio:.0f}")

        # ── north arrow (top-right of drawing area) ──────────────────────────
        na_cx = DRAW_X + DRAW_W - 18
        na_cy = DRAW_Y + DRAW_H - 20
        na_r  = 9
        canv.setStrokeColor(_rl_colors.black)
        canv.setLineWidth(0.8)
        canv.circle(na_cx, na_cy, na_r, fill=0, stroke=1)
        canv.setLineWidth(1.1)
        canv.line(na_cx, na_cy - na_r + 3, na_cx, na_cy + na_r - 2)
        path = canv.beginPath()
        path.moveTo(na_cx, na_cy + na_r - 2)
        path.lineTo(na_cx - 3, na_cy + 1)
        path.lineTo(na_cx + 3, na_cy + 1)
        path.close()
        canv.setFillColor(_rl_colors.black)
        canv.drawPath(path, fill=1, stroke=0)
        canv.setFont("Helvetica-Bold", 7)
        canv.drawCentredString(na_cx, na_cy - na_r - 6, "N")

    def _fmt_ftin(meters: float) -> str:
        """Format metres as feet-inch string, e.g. 2.74 → 9'-0\". Handles negatives."""
        sign     = "-" if meters < 0 else ""
        total_in = abs(meters) * 39.3701
        feet     = int(total_in // 12)
        inch     = int(round(total_in % 12))
        if inch == 12:
            feet += 1; inch = 0
        return f"{sign}{feet}'-{inch}\""

    def _fmt_inches(meters: float) -> str:
        """Format metres as inch string with fractions (nearest 1/8), e.g. 2.095 → 82 1/2\"."""
        total_in = abs(meters) * 39.3701
        whole    = int(total_in)
        eighths  = round((total_in - whole) * 8)
        if eighths == 8:
            whole += 1; eighths = 0
        if eighths == 0:
            return f'{whole}"'
        g = math.gcd(eighths, 8)
        return f'{whole} {eighths // g}/{8 // g}"'

    # ── page renderer — draws actual member hulls + name labels beside panels ─
    def _draw_page(title: str, panel_items: list, show_heights: bool = False) -> None:
        """
        panel_items: [{"name": str, "bucket": str,
                        "hulls": [[(x,y), ...], ...]}]
        Each hull is the convex hull of one entity/member in the panel.
        show_heights: draw ft/in height scale on the left (for elevation pages).
        """
        if not panel_items:
            return
        _new_page(title)

        all_pts = [(x, y) for item in panel_items
                   for h in item["hulls"] for x, y in h]
        if not all_pts:
            return
        ax0 = min(p[0] for p in all_pts)
        ay0 = min(p[1] for p in all_pts)
        ax1 = max(p[0] for p in all_pts)
        ay1 = max(p[1] for p in all_pts)
        rx  = ax1 - ax0 or 1.0
        ry  = ay1 - ay0 or 1.0

        ht_reserve  = 28 if show_heights else 0   # pts on left for height scale
        pad         = 22
        lbl_reserve = 54
        aw    = DRAW_W - 2 * pad - lbl_reserve - ht_reserve
        ah    = DRAW_H - 2 * pad
        scale = min(aw / rx, ah / ry)
        ox    = DRAW_X + pad + ht_reserve + (aw - rx * scale) / 2
        oy    = DRAW_Y + pad + (ah - ry * scale) / 2

        def pdf(x: float, y: float) -> tuple:
            return ox + (x - ax0) * scale, oy + (y - ay0) * scale

        pending_labels: list = []   # (px_max, ly_ideal, name, r, g, b)

        for item in panel_items:
            r, g, b = BUCKET_RGB.get(item["bucket"], (0, 0, 0))
            fill_r = r * 0.06 + 0.94
            fill_g = g * 0.06 + 0.94
            fill_b = b * 0.06 + 0.94

            px_max = -1e18
            py_min =  1e18
            py_max = -1e18

            for hull in item["hulls"]:
                if not hull:
                    continue
                pts_pdf = [pdf(x, y) for x, y in hull]

                for px, py in pts_pdf:
                    if px > px_max: px_max = px
                    if py < py_min: py_min = py
                    if py > py_max: py_max = py

                canv.setFillColorRGB(fill_r, fill_g, fill_b)
                canv.setStrokeColorRGB(r, g, b)
                canv.setLineWidth(0.55)

                if len(pts_pdf) >= 3:
                    path = canv.beginPath()
                    path.moveTo(*pts_pdf[0])
                    for pt in pts_pdf[1:]:
                        path.lineTo(*pt)
                    path.close()
                    canv.drawPath(path, fill=1, stroke=1)
                elif len(pts_pdf) == 2:
                    canv.setLineWidth(1.1)
                    canv.setStrokeColorRGB(r, g, b)
                    canv.line(pts_pdf[0][0], pts_pdf[0][1],
                              pts_pdf[1][0], pts_pdf[1][1])

            if item["name"] and px_max > -1e17:
                ly = (py_min + py_max) / 2
                pending_labels.append((px_max, ly, item["name"], r, g, b))

        # Draw labels after geometry — sort by y and push apart to avoid overlap
        FS        = 5.5
        MIN_GAP   = FS + 1.5   # minimum vertical gap between label baselines
        lbl_bot   = DRAW_Y + 2
        lbl_top   = DRAW_Y + DRAW_H - 4
        pending_labels.sort(key=lambda t: t[1])
        placed_y: list[float] = []
        for px_max, ly_ideal, name, r, g, b in pending_labels:
            ly = ly_ideal
            # Push up past any label that would overlap
            for prev_y in reversed(placed_y):
                if ly - prev_y < MIN_GAP:
                    ly = prev_y + MIN_GAP
                else:
                    break
            placed_y.append(ly)
        # Shift entire stack down if labels escaped the drawing area at the top
        if placed_y and placed_y[-1] > lbl_top:
            shift = placed_y[-1] - lbl_top
            placed_y = [max(lbl_bot, y - shift) for y in placed_y]
        for (px_max, ly_ideal, name, r, g, b), ly in zip(pending_labels, placed_y):
            label_text = name[:20]
            tw = canv.stringWidth(label_text, "Helvetica", FS)
            right_limit = DRAW_X + DRAW_W - 4
            lx = min(px_max + 4, right_limit - tw)
            canv.setLineWidth(0.3)
            canv.setStrokeColorRGB(0.55, 0.55, 0.55)
            canv.line(px_max, ly_ideal, lx - 1, ly_ideal)
            if abs(ly - ly_ideal) > 1:
                canv.line(lx - 1, ly_ideal, lx - 1, ly)
            hp = 1.5
            canv.setFillColor(_rl_colors.white)
            canv.rect(lx - hp, ly - FS * 0.82, tw + hp * 2, FS + hp, fill=1, stroke=0)
            canv.setFont("Helvetica", FS)
            canv.setFillColorRGB(0.05, 0.05, 0.05)
            canv.drawString(lx, ly - FS * 0.36, label_text)

        # ── height scale (ft/in) on the left for elevation views ─────────────
        if show_heights:
            # Pick tick interval: smallest that gives >= 9 pts spacing
            for iv_m in [0.3048, 0.6096, 0.9144, 1.2192, 1.8288, 3.048]:
                if iv_m * scale >= 9:
                    tick_m = iv_m
                    break
            else:
                tick_m = 3.048

            scx = DRAW_X + pad + ht_reserve - 4   # x of the vertical rule
            ground_z = ay0                          # world Z that maps to 0 height
            # vertical rule
            canv.setStrokeColorRGB(0.35, 0.35, 0.35)
            canv.setLineWidth(0.5)
            canv.line(scx, oy, scx, oy + (ay1 - ay0) * scale)

            z = math.ceil(ay0 / tick_m) * tick_m
            while z <= ay1 + 1e-6:
                py     = oy + (z - ay0) * scale
                h_abv  = z - ground_z
                label  = _fmt_ftin(h_abv)
                canv.setLineWidth(0.4)
                canv.setStrokeColorRGB(0.35, 0.35, 0.35)
                canv.line(scx - 3, py, scx, py)
                canv.setFont("Helvetica", 5.0)
                canv.setFillColorRGB(0.15, 0.15, 0.15)
                canv.drawRightString(scx - 4, py - 1.8, label)
                z += tick_m

        # scale annotation
        mm_per_pt = 25.4 / 72
        ratio     = 1000 / (scale * mm_per_pt)
        canv.setFont("Helvetica", 6.5)
        canv.setFillColor(_rl_colors.black)
        canv.drawString(DRAW_X + 4, DRAW_Y + 4, f"Scale  1 : {ratio:.0f}")

    def _draw_combined_plan_page(title: str, pid_set: set) -> None:
        """Combined floor plan: wall bboxes (with openings) + truss convex hulls."""
        wall_items  = _wall_plan_items(pid_set, {"Walls"})
        truss_items = _panel_items(pid_set, {"Trusses"}, "plan")
        if not wall_items and not truss_items:
            return
        _new_page(title)

        # unified coordinate bounds from both sets
        bpts: list = []
        for it in wall_items:
            bx0, by0, bx1, by1 = it["bbox"]
            bpts += [(bx0, by0), (bx1, by1)]
        for it in truss_items:
            for h in it["hulls"]:
                bpts += h
        if not bpts:
            return

        ax0 = min(p[0] for p in bpts); ax1 = max(p[0] for p in bpts)
        ay0 = min(p[1] for p in bpts); ay1 = max(p[1] for p in bpts)
        rx = ax1 - ax0 or 1.0
        ry = ay1 - ay0 or 1.0

        pad         = 22
        lbl_reserve = 64
        aw    = DRAW_W - 2 * pad - lbl_reserve
        ah    = DRAW_H - 2 * pad
        scale = min(aw / rx, ah / ry)
        ox_off = DRAW_X + pad + (aw - rx * scale) / 2
        oy_off = DRAW_Y + pad + (ah - ry * scale) / 2
        lx0   = ox_off + rx * scale + 5   # label column left edge

        def cpdf(wx: float, wy: float) -> tuple:
            return ox_off + (wx - ax0) * scale, oy_off + (wy - ay0) * scale

        pending: list = []  # (px_max, py_mid, name, r, g, b)

        # walls + floors as filled bboxes
        for item in wall_items:
            r, g, b = BUCKET_RGB.get(item["bucket"], (0, 0, 0))
            bx0i, by0i, bx1i, by1i = item["bbox"]
            px0, py0 = cpdf(bx0i, by0i)
            pw = (bx1i - bx0i) * scale
            ph = (by1i - by0i) * scale
            if pw < 0.4 or ph < 0.4:
                continue
            canv.setFillColorRGB(r * 0.05 + 0.95, g * 0.05 + 0.95, b * 0.05 + 0.95)
            canv.setStrokeColorRGB(r, g, b)
            canv.setLineWidth(0.55)
            canv.rect(px0, py0, pw, ph, fill=1, stroke=1)
            for ox0b, oy0b, ox1b, oy1b in item.get("op_bboxes", []):
                opx0, opy0 = cpdf(ox0b, oy0b)
                opw = (ox1b - ox0b) * scale
                oph = (oy1b - oy0b) * scale
                if opw > 0.4 and oph > 0.4:
                    canv.setFillColor(_rl_colors.white)
                    canv.setStrokeColorRGB(0.6, 0.6, 0.6)
                    canv.setLineWidth(0.3)
                    canv.rect(opx0, opy0, opw, oph, fill=1, stroke=1)
            if item["name"]:
                pending.append((px0 + pw, py0 + ph / 2, item["name"], r, g, b))

        # trusses as convex hulls drawn on top
        for item in truss_items:
            r, g, b = BUCKET_RGB.get(item["bucket"], (0, 0, 0))
            fill_r = r * 0.14 + 0.86
            fill_g = g * 0.14 + 0.86
            fill_b = b * 0.14 + 0.86
            px_max = -1e18; py_sum = 0.0; py_cnt = 0
            for hull in item["hulls"]:
                if not hull:
                    continue
                pts = [cpdf(x, y) for x, y in hull]
                for px, py in pts:
                    if px > px_max: px_max = px
                    py_sum += py; py_cnt += 1
                canv.setFillColorRGB(fill_r, fill_g, fill_b)
                canv.setStrokeColorRGB(r, g, b)
                canv.setLineWidth(0.7)
                if len(pts) >= 3:
                    path = canv.beginPath()
                    path.moveTo(*pts[0])
                    for p in pts[1:]:
                        path.lineTo(*p)
                    path.close()
                    canv.drawPath(path, fill=1, stroke=1)
                elif len(pts) == 2:
                    canv.setLineWidth(1.2)
                    canv.line(pts[0][0], pts[0][1], pts[1][0], pts[1][1])
            if item["name"] and px_max > -1e17 and py_cnt:
                pending.append((px_max, py_sum / py_cnt, item["name"], r, g, b))

        # collision-aware label column
        FS      = 5.0
        MIN_GAP = FS + 1.2
        lbl_bot = DRAW_Y + 2
        lbl_top = DRAW_Y + DRAW_H - 4
        pending.sort(key=lambda t: t[1])
        placed_y: list[float] = []
        for _px, ly_ideal, _nm, _r, _g, _b in pending:
            ly = ly_ideal
            for prev_y in reversed(placed_y):
                if ly - prev_y < MIN_GAP:
                    ly = prev_y + MIN_GAP
                else:
                    break
            placed_y.append(ly)
        if placed_y and placed_y[-1] > lbl_top:
            shift = placed_y[-1] - lbl_top
            placed_y = [max(lbl_bot, y - shift) for y in placed_y]
        for (px_max, ly_ideal, name, r, g, b), ly in zip(pending, placed_y):
            canv.setLineWidth(0.25)
            canv.setStrokeColorRGB(0.6, 0.6, 0.6)
            canv.line(px_max, ly_ideal, lx0 - 1, ly_ideal)
            if abs(ly - ly_ideal) > 0.5:
                canv.line(lx0 - 1, ly_ideal, lx0 - 1, ly)
            canv.setFont("Helvetica", FS)
            canv.setFillColorRGB(r, g, b)
            canv.drawString(lx0, ly - FS * 0.35, name[:22])

        mm_per_pt = 25.4 / 72
        ratio = 1000 / (scale * mm_per_pt)
        canv.setFont("Helvetica", 6.5)
        canv.setFillColor(_rl_colors.black)
        canv.drawString(DRAW_X + 4, DRAW_Y + 4, f"Scale  1 : {ratio:.0f}")

        # ── north arrow (label column, near top) ─────────────────────────────
        na_cx = min(lx0 + 28, DRAW_X + DRAW_W - 12)
        na_cy = DRAW_Y + DRAW_H - 24
        na_r  = 9
        canv.setStrokeColor(_rl_colors.black)
        canv.setLineWidth(0.8)
        canv.circle(na_cx, na_cy, na_r, fill=0, stroke=1)
        canv.setLineWidth(1.1)
        canv.line(na_cx, na_cy - na_r + 3, na_cx, na_cy + na_r - 2)
        path = canv.beginPath()
        path.moveTo(na_cx, na_cy + na_r - 2)
        path.lineTo(na_cx - 3, na_cy + 1)
        path.lineTo(na_cx + 3, na_cy + 1)
        path.close()
        canv.setFillColor(_rl_colors.black)
        canv.drawPath(path, fill=1, stroke=0)
        canv.setFont("Helvetica-Bold", 7)
        canv.drawCentredString(na_cx, na_cy - na_r - 6, "N")

        # ── overall width dimension (bottom of plan content) ─────────────────
        wdim_y = DRAW_Y + 14
        lpx, _ = cpdf(ax0, ay0)
        rpx, _ = cpdf(ax1, ay0)
        if rpx - lpx > 10:
            canv.setStrokeColorRGB(0.25, 0.25, 0.25)
            canv.setLineWidth(0.4)
            canv.line(lpx, wdim_y - 2, lpx, wdim_y + 7)
            canv.line(rpx, wdim_y - 2, rpx, wdim_y + 7)
            canv.line(lpx, wdim_y + 3, rpx, wdim_y + 3)
            w_ft = (ax1 - ax0) / 0.3048
            wft = int(w_ft); win = int(round((w_ft - wft) * 12))
            if win == 12: wft += 1; win = 0
            canv.setFont("Helvetica-Bold", 6)
            canv.setFillColor(_rl_colors.black)
            canv.drawCentredString((lpx + rpx) / 2, wdim_y + 5, f"{wft}'-{win}\"")

    # ── architectural elevation: clean bbox rectangles, height scale, dim ────
    def _draw_elevation_page(title: str, elev_items: list) -> None:
        if not elev_items:
            return
        _new_page(title)

        all_bpts = [(it["bbox"][0], it["bbox"][1]) for it in elev_items] + \
                   [(it["bbox"][2], it["bbox"][3]) for it in elev_items]
        if not all_bpts:
            return
        ax0 = min(p[0] for p in all_bpts); ax1 = max(p[0] for p in all_bpts)
        ay0 = min(p[1] for p in all_bpts); ay1 = max(p[1] for p in all_bpts)
        rx  = ax1 - ax0 or 1.0
        ry  = ay1 - ay0 or 1.0

        HT_W  = 38   # left height-scale column
        DIM_H = 20   # bottom dimension row
        pad   = 14
        aw    = DRAW_W - HT_W - 2 * pad
        ah    = DRAW_H - 2 * pad - DIM_H
        scale = min(aw / rx, ah / ry)
        ox    = DRAW_X + HT_W + pad + (aw - rx * scale) / 2
        oy    = DRAW_Y + DIM_H + pad + (ah - ry * scale) / 2

        def epdf(h: float, z: float) -> tuple:
            return ox + (h - ax0) * scale, oy + (z - ay0) * scale

        # Painter's algorithm: back-to-front by sort_depth (lowest = furthest from camera)
        for item in sorted(elev_items, key=lambda it: it.get("sort_depth", 0)):
            h0, z0, h1, z1 = item["bbox"]
            px, pz = epdf(h0, z0)
            pw = (h1 - h0) * scale
            ph = (z1 - z0) * scale
            if pw < 0.3 or ph < 0.3:
                continue
            # White backing plate — covers everything drawn behind this panel
            canv.setFillColor(_rl_colors.white)
            canv.setStrokeColor(_rl_colors.white)
            canv.rect(px, pz, pw, ph, fill=1, stroke=0)
            # Per-entity structural members drawn on top of the white plate
            r, g, b = BUCKET_RGB.get(item["bucket"], (0, 0, 0))
            fill_r = r * 0.08 + 0.92
            fill_g = g * 0.08 + 0.92
            fill_b = b * 0.08 + 0.92
            stroke_r = max(0.0, r * 0.7)
            stroke_g = max(0.0, g * 0.7)
            stroke_b = max(0.0, b * 0.7)
            entity_bboxes = item.get("entity_bboxes", [])
            if entity_bboxes:
                for eh0, ez0, eh1, ez1 in entity_bboxes:
                    epx2, epz2 = epdf(eh0, ez0)
                    epw2 = (eh1 - eh0) * scale
                    eph2 = (ez1 - ez0) * scale
                    if epw2 < 0.25 or eph2 < 0.25:
                        continue
                    canv.setFillColorRGB(fill_r, fill_g, fill_b)
                    canv.setStrokeColorRGB(stroke_r, stroke_g, stroke_b)
                    canv.setLineWidth(0.5)
                    canv.rect(epx2, epz2, epw2, eph2, fill=1, stroke=1)
            else:
                canv.setFillColorRGB(fill_r, fill_g, fill_b)
                canv.setStrokeColorRGB(stroke_r, stroke_g, stroke_b)
                canv.setLineWidth(0.6)
                canv.rect(px, pz, pw, ph, fill=1, stroke=1)
            for oh0, oz0, oh1, oz1 in item.get("op_bboxes", []):
                opx, opz = epdf(oh0, oz0)
                opw = (oh1 - oh0) * scale
                oph = (oz1 - oz0) * scale
                if opw > 0.3 and oph > 0.3:
                    canv.setFillColor(_rl_colors.white)
                    canv.setStrokeColorRGB(0.65, 0.65, 0.65)
                    canv.setLineWidth(0.3)
                    canv.rect(opx, opz, opw, oph, fill=1, stroke=1)

        # Height scale (left side)
        scx = DRAW_X + HT_W - 4
        py_bot = oy
        py_top = oy + ry * scale
        canv.setLineWidth(0.5)
        canv.setStrokeColorRGB(0.3, 0.3, 0.3)
        canv.line(scx, py_bot, scx, py_top)
        tick_m = 0.3048   # every 1 ft
        z = math.ceil(ay0 / tick_m) * tick_m
        while z <= ay1 + 1e-6:
            py = oy + (z - ay0) * scale
            canv.setLineWidth(0.4)
            canv.line(scx - 3, py, scx, py)
            canv.setFont("Helvetica", 5.0)
            canv.setFillColorRGB(0.15, 0.15, 0.15)
            canv.drawRightString(scx - 4, py - 1.8, _fmt_ftin(z - ay0))
            z += tick_m

        # Overall width dimension (bottom)
        dpy = DRAW_Y + DIM_H - 5
        lp, _ = epdf(ax0, ay0)
        rp, _ = epdf(ax1, ay0)
        if rp - lp > 10:
            canv.setStrokeColorRGB(0.2, 0.2, 0.2)
            canv.setLineWidth(0.4)
            canv.line(lp, dpy - 1, lp, dpy + 8)
            canv.line(rp, dpy - 1, rp, dpy + 8)
            canv.line(lp, dpy + 3, rp, dpy + 3)
            w_ft = (ax1 - ax0) / 0.3048
            wft = int(w_ft); win = int(round((w_ft - wft) * 12))
            if win == 12: wft += 1; win = 0
            canv.setFont("Helvetica-Bold", 6.5)
            canv.setFillColor(_rl_colors.black)
            canv.drawCentredString((lp + rp) / 2, dpy + 5, f"{wft}'-{win}\"")

        # Overall height dimension (right side)
        hbp, _ = epdf(ax1, ay0)
        htp, _ = epdf(ax1, ay1)
        hdx = hbp + 12
        _, hb_y = epdf(ax0, ay0)
        _, ht_y = epdf(ax0, ay1)
        if ht_y - hb_y > 10 and hdx < DRAW_X + DRAW_W - 4:
            canv.setLineWidth(0.4)
            canv.line(hdx - 3, hb_y, hdx + 3, hb_y)
            canv.line(hdx - 3, ht_y, hdx + 3, ht_y)
            canv.line(hdx, hb_y, hdx, ht_y)
            h_ft = (ay1 - ay0) / 0.3048
            hft = int(h_ft); hin = int(round((h_ft - hft) * 12))
            if hin == 12: hft += 1; hin = 0
            canv.saveState()
            canv.translate(hdx + 7, (hb_y + ht_y) / 2)
            canv.rotate(90)
            canv.setFont("Helvetica-Bold", 6.5)
            canv.setFillColor(_rl_colors.black)
            canv.drawCentredString(0, 0, f"{hft}'-{hin}\"")
            canv.restoreState()

        mm_per_pt = 25.4 / 72
        ratio = 1000 / (scale * mm_per_pt)
        canv.setFont("Helvetica", 6.5)
        canv.setFillColor(_rl_colors.black)
        canv.drawString(DRAW_X + 4, DRAW_Y + 4, f"Scale  1 : {ratio:.0f}")

    # ── geometry extraction ───────────────────────────────────────────────────
    settings = ifcopenshell.geom.settings()
    try:
        settings.set("use-world-coords", True)
    except Exception:
        pass

    all_products  = [e for e in model.by_type("IfcProduct")
                     if getattr(e, "Representation", None)]
    main_include  = [e for e in all_products if not e.is_a("IfcOpeningElement")]
    num_threads   = max(1, min(4, os.cpu_count() or 1))
    iterator      = ifcopenshell.geom.iterator(settings, model, num_threads,
                                               include=main_include)

    geom:       dict[int, tuple]  = {}
    entity_map: dict[int, object] = {}
    if iterator.initialize():
        while True:
            shape = iterator.get()
            ent   = model.by_id(shape.id)
            verts = tuple(getattr(getattr(shape, "geometry", None), "verts", ()) or ())
            if len(verts) >= 3:
                geom[shape.id]       = verts
                entity_map[shape.id] = ent
            if not iterator.next():
                break

    # Opening geometry (voids / door-window rough openings)
    opening_verts_map: dict[int, tuple] = {}
    op_include = [e for e in all_products if e.is_a("IfcOpeningElement")]
    if op_include:
        op_it = ifcopenshell.geom.iterator(settings, model, 1, include=op_include)
        if op_it.initialize():
            while True:
                shape = op_it.get()
                verts = tuple(getattr(getattr(shape, "geometry", None), "verts", ()) or ())
                if len(verts) >= 3:
                    opening_verts_map[shape.id] = verts
                if not op_it.next():
                    break

    # ── panel grouping — entity verts stored per entity id ───────────────────
    # panel_id → {name, bucket, entities: {eid: verts_flat}}
    panels: dict[int, dict] = {}
    for eid, verts in geom.items():
        ent    = entity_map[eid]
        parent = _get_parent_assembly(ent)
        if parent:
            pid   = parent.id()
            pname = getattr(parent, "Name", None) or "Assembly"
            pbkt  = _component_bucket(parent)
        else:
            pid   = eid
            pname = getattr(ent, "Name", None) or "Unnamed"
            pbkt  = _component_bucket(ent)
        if pid not in panels:
            panels[pid] = {"name": pname, "bucket": pbkt, "entities": {}}
        panels[pid]["entities"][eid] = verts

    # Map panel_id → opening verts lists (via IfcRelVoidsElement)
    panel_opening_verts: dict[int, list] = {}
    for rel in model.by_type("IfcRelVoidsElement"):
        wall_eid    = rel.RelatingBuildingElement.id()
        opening_eid = rel.RelatedOpeningElement.id()
        ov = opening_verts_map.get(opening_eid)
        if ov is None:
            continue
        ent = entity_map.get(wall_eid)
        if ent is None:
            continue
        parent = _get_parent_assembly(ent)
        pid = parent.id() if parent else wall_eid
        panel_opening_verts.setdefault(pid, []).append(ov)

    # ── bottom-Z per panel (used for level grouping) ─────────────────────────
    panel_bottom_z: dict[int, float] = {}
    for pid, p in panels.items():
        min_z = float("inf")
        for vf in p["entities"].values():
            for i in range(2, len(vf), 3):
                if vf[i] < min_z:
                    min_z = vf[i]
        panel_bottom_z[pid] = min_z if min_z < float("inf") else 0.0

    # Cluster a pid set by bottom-Z (gap > 0.4 m → new level)
    def _z_clusters(pid_set: set) -> list:
        if not pid_set:
            return []
        ordered = sorted(pid_set, key=lambda p: panel_bottom_z.get(p, 0.0))
        clusters: list[list] = [[ordered[0]]]
        for pid in ordered[1:]:
            last_z = panel_bottom_z.get(clusters[-1][-1], 0.0)
            if panel_bottom_z.get(pid, 0.0) - last_z > 0.4:
                clusters.append([])
            clusters[-1].append(pid)
        return [(panel_bottom_z[c[0]], set(c)) for c in clusters]

    # ── storey grouping with Z sub-division ───────────────────────────────────
    ifc_storey_pids: dict[tuple, set] = {}
    for rel in model.by_type("IfcRelContainedInSpatialStructure"):
        storey = rel.RelatingStructure
        if not storey.is_a("IfcBuildingStorey"):
            continue
        sname = getattr(storey, "Name", None) or f"Level {storey.id()}"
        elev  = float(getattr(storey, "Elevation", 0) or 0)
        key   = (elev, sname)
        ifc_storey_pids.setdefault(key, set())
        for ent in rel.RelatedElements:
            parent = _get_parent_assembly(ent)
            pid    = parent.id() if parent else ent.id()
            if pid in panels:
                ifc_storey_pids[key].add(pid)

    # Expand each IFC storey into Z-level sub-pages where needed
    final_storeys: list[tuple] = []   # (sort_key, label, pid_set)
    for (elev, sname), pids in sorted(ifc_storey_pids.items()):
        z_groups = _z_clusters(pids)
        if len(z_groups) <= 1:
            final_storeys.append((elev, sname, pids))
        else:
            for z0, sub_pids in z_groups:
                final_storeys.append((elev + z0, f"{sname} @ {_fmt_ftin(z0)}", sub_pids))

    # Leftover panels (not in any IFC storey) → cluster by Z
    assigned = {pid for _, _, pids in final_storeys for pid in pids}
    leftover  = set(panels) - assigned
    if leftover:
        for i, (z0, sub_pids) in enumerate(_z_clusters(leftover), 1):
            final_storeys.append((z0, f"Level {i}", sub_pids))

    final_storeys.sort(key=lambda t: t[0])

    # ── helpers: wall-plan items (bbox per panel + opening cutouts) ──────────
    def _wall_plan_items(pid_set: set, buckets: set) -> list:
        result = []
        for pid in pid_set:
            p = panels.get(pid)
            if not p or p["bucket"] not in buckets:
                continue
            xs, ys = [], []
            for vf in p["entities"].values():
                for i in range(0, len(vf) - 2, 3):
                    xs.append(vf[i]); ys.append(vf[i + 1])
            if not xs:
                continue
            op_bboxes = []
            for ov in panel_opening_verts.get(pid, []):
                oxs, oys = [], []
                for i in range(0, len(ov) - 2, 3):
                    oxs.append(ov[i]); oys.append(ov[i + 1])
                if oxs:
                    op_bboxes.append((min(oxs), min(oys), max(oxs), max(oys)))
            result.append({
                "name":      p["name"],
                "bucket":    p["bucket"],
                "bbox":      (min(xs), min(ys), max(xs), max(ys)),
                "op_bboxes": op_bboxes,
            })
        return result

    # ── helpers: hull-based items (trusses, plan views) ──────────────────────
    def _panel_items(pid_set: set, buckets: set, view: str) -> list:
        result = []
        for pid in pid_set:
            p = panels.get(pid)
            if not p or p["bucket"] not in buckets:
                continue
            hulls = []
            for verts_flat in p["entities"].values():
                pts = _project(verts_flat, view)
                h   = _hull(pts)
                if h:
                    hulls.append(h)
            if hulls:
                result.append({"name": p["name"], "bucket": p["bucket"], "hulls": hulls})
        return result

    def _elevation_bbox_items(pid_set: set, buckets: set, direction: str) -> list:
        """Per-panel bounding boxes + painter-sort depth for back-to-front rendering."""
        # sign: lower sort_depth = farther from camera = drawn first
        depth_sign = -1.0 if direction in ("south", "west") else 1.0
        result = []
        for pid in pid_set:
            p = panels.get(pid)
            if not p or p["bucket"] not in buckets:
                continue
            hs, zs, ds = [], [], []
            entity_bboxes = []
            for vf in p["entities"].values():
                ehs, ezs = [], []
                for i in range(0, len(vf) - 2, 3):
                    x, y, z = vf[i], vf[i + 1], vf[i + 2]
                    if   direction == "north": h_val = x;  d_val = y
                    elif direction == "south": h_val = -x; d_val = y
                    elif direction == "east":  h_val = -y; d_val = x
                    else:                      h_val = y;  d_val = x
                    ehs.append(h_val); hs.append(h_val)
                    ds.append(d_val)
                    ezs.append(z);     zs.append(z)
                if ehs:
                    entity_bboxes.append((min(ehs), min(ezs), max(ehs), max(ezs)))
            if not hs:
                continue
            sort_depth = (sum(ds) / len(ds)) * depth_sign
            op_bboxes = []
            for ov in panel_opening_verts.get(pid, []):
                ohs, ozs = [], []
                for i in range(0, len(ov) - 2, 3):
                    x, y, z = ov[i], ov[i + 1], ov[i + 2]
                    if   direction == "north": ohs.append(x)
                    elif direction == "south": ohs.append(-x)
                    elif direction == "east":  ohs.append(-y)
                    else:                      ohs.append(y)
                    ozs.append(z)
                if ohs:
                    op_bboxes.append((min(ohs), min(ozs), max(ohs), max(ozs)))
            result.append({
                "name":          p["name"],
                "bucket":        p["bucket"],
                "bbox":          (min(hs), min(zs), max(hs), max(zs)),
                "entity_bboxes": entity_bboxes,
                "op_bboxes":     op_bboxes,
                "sort_depth":    sort_depth,
            })
        return result

    # ── elevation occlusion + roof/truss selection ────────────────────────────
    def _elevation_pids(wall_cands: set, roof_cands: set, truss_cands: set,
                        floor_cands: set, direction: str) -> tuple:
        """
        Returns (visible_walls, visible_roofs, visible_trusses, visible_floors).

        Walls   — per-1ft scan band, keep only the frontmost wall(s) (≤1.0 m depth
                  tolerance).  Any wall entirely behind another in the same band is
                  dropped.
        Roofs   — only roofs whose span overlaps a visible wall AND that survive
                  their own per-band depth occlusion.
        Trusses — only trusses whose span overlaps a visible wall AND that have no
                  visible roof panel above them in the same span.
        Floors  — all floors whose span overlaps any visible wall (fills the
                  level-transition gap between storeys in the elevation).
        """
        BAND     = 0.3048          # 1 foot
        want_max = direction in ("E", "N")

        def _info(pid: int) -> dict | None:
            xs, ys, zs = [], [], []
            for vf in panels[pid]["entities"].values():
                for i in range(0, len(vf) - 2, 3):
                    xs.append(vf[i]); ys.append(vf[i + 1]); zs.append(vf[i + 2])
            if not xs:
                return None
            if direction in ("E", "W"):
                depth = sum(xs) / len(xs)
                slo, shi = min(ys), max(ys)
            else:
                depth = sum(ys) / len(ys)
                slo, shi = min(xs), max(xs)
            return {"depth": depth, "slo": slo, "shi": shi,
                    "zlo": min(zs), "zhi": max(zs)}

        def _scanline(pid_set: set, tol: float = 1.0) -> tuple[set, dict]:
            """Depth-occlusion scan. tol=1.0 m for walls/roofs, ~0 for trusses."""
            imap = {pid: inf for pid in pid_set
                    if (inf := _info(pid)) is not None}
            if not imap:
                return set(), {}
            slo = min(v["slo"] for v in imap.values())
            shi = max(v["shi"] for v in imap.values())
            vis: set = set()
            pos = slo
            while pos < shi:
                bhi = pos + BAND
                band = [p for p, v in imap.items()
                        if v["slo"] < bhi and v["shi"] > pos]
                if band:
                    ext = (max(imap[p]["depth"] for p in band) if want_max
                           else min(imap[p]["depth"] for p in band))
                    for p in band:
                        if abs(imap[p]["depth"] - ext) <= tol:
                            vis.add(p)
                pos += BAND
            return vis or set(imap), imap

        # Stage 1 — visible walls (3 m tolerance captures setbacks while excluding interior walls)
        vis_walls, wall_imap = _scanline(wall_cands, tol=3.0)
        if not vis_walls:
            return wall_cands, set(), set(), set()

        vis_spans = [(wall_imap[p]["slo"], wall_imap[p]["shi"]) for p in vis_walls]

        def _overlaps_walls(slo: float, shi: float) -> bool:
            return any(slo < wshi and shi > wslo for wslo, wshi in vis_spans)

        # Stage 2 — roofs that touch a visible wall (span overlap), then occluded
        roof_cands_filtered = {p for p in roof_cands
                                if (inf := _info(p)) is not None
                                and _overlaps_walls(inf["slo"], inf["shi"])}
        vis_roofs, roof_imap = _scanline(roof_cands_filtered, tol=3.0)

        # Stage 3 — trusses: span overlaps walls, frontmost per band only (tol≈0),
        #           then drop any truss that has a visible roof above it in span.
        truss_span_filtered = {p for p in truss_cands
                                if (inf := _info(p)) is not None
                                and _overlaps_walls(inf["slo"], inf["shi"])}
        # tol=0.05 m keeps only the frontmost truss in each 1-ft column
        front_trusses, truss_imap = _scanline(truss_span_filtered, tol=0.05)
        vis_trusses: set = set()
        for pid in front_trusses:
            inf = truss_imap[pid]
            has_roof = any(
                ri["slo"] < inf["shi"] and ri["shi"] > inf["slo"]
                and ri["zlo"] >= inf["zhi"] - 0.3
                for rpid in vis_roofs if (ri := roof_imap.get(rpid)) is not None
            )
            if not has_roof:
                vis_trusses.add(pid)

        # Stage 4 — floors whose horizontal span overlaps a visible wall span.
        # These fill the level-transition gap between storeys in the elevation.
        vis_floors: set = {p for p in floor_cands
                           if (inf := _info(p)) is not None
                           and _overlaps_walls(inf["slo"], inf["shi"])}

        return vis_walls, vis_roofs, vis_trusses, vis_floors

    # ── WALL PLANS + TRUSS PLANS + FLOOR TRUSS PLANS — separate pages per storey ─
    for _sort_key, sname, pids in final_storeys:
        wall_its = _wall_plan_items(pids, {"Walls"})
        if wall_its:
            _draw_wall_plan_page(f"Wall Plan  {sname}", wall_its)
        truss_its = _panel_items(pids, {"Trusses"}, "plan")
        if truss_its:
            _draw_page(f"Truss Plan  {sname}", truss_its)
        floor_its = _panel_items(pids, {"Floors"}, "plan")
        if floor_its:
            _draw_page(f"Floor Truss Plan  {sname}", floor_its)

    # ── 4 ELEVATIONS — clean bbox rectangles, staged occlusion ───────────────
    wall_pids  = {pid for pid, p in panels.items() if p["bucket"] == "Walls"}
    roof_pids  = {pid for pid, p in panels.items() if p["bucket"] == "Roofs"}
    truss_pids = {pid for pid, p in panels.items() if p["bucket"] == "Trusses"}
    floor_pids = {pid for pid, p in panels.items() if p["bucket"] == "Floors"}

    # Painter's algorithm handles all occlusion — pass every panel for each elevation
    all_pids = wall_pids | roof_pids | truss_pids | floor_pids

    for direction, label in [
        ("north", "North Elevation"),
        ("south", "South Elevation"),
        ("east",  "East Elevation"),
        ("west",  "West Elevation"),
    ]:
        _draw_elevation_page(
            label,
            _elevation_bbox_items(all_pids, {"Walls", "Roofs", "Trusses", "Floors"}, direction)
        )

    canv.save()
    return buf.getvalue()


@app.get("/")
def index():
    return render_template("builder.html")


@app.get("/api/builder/health")
def health():
    return jsonify({"status": "ok", "view": "builder"})


@app.post("/api/builder/preview")
def builder_preview():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"error": "No file uploaded."}), 400

    filename = _sanitize_filename(upload.filename)
    if Path(filename).suffix.lower() != ".ifc":
        return jsonify({"error": "BisonBuilder requires a .ifc file."}), 400

    upload_id = uuid.uuid4().hex
    ifc_path = UPLOAD_DIR / f"{upload_id}_{filename}"
    upload.save(ifc_path)

    try:
        payload = _build_builder_payload(ifc_path)
        return jsonify(payload)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        ifc_path.unlink(missing_ok=True)


@app.post("/api/builder/convert")
def builder_convert():
    if not _CONVERTER_AVAILABLE:
        return jsonify({"error": "Converter not available in this installation."}), 503

    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"error": "No file uploaded."}), 400

    filename = _sanitize_filename(upload.filename)
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_SOURCE_EXTS:
        return jsonify({"error": "Unsupported file type. Use .dxf or .ifc"}), 400

    upload_id = uuid.uuid4().hex
    upload_path = UPLOAD_DIR / f"{upload_id}_{filename}"
    upload.save(upload_path)

    try:
        options = ConversionOptions(
            include_walls=True,
            include_ceiling=True,
            include_roof=True,
            include_floor=False,
            include_trusses=True,
        )
        output_name = f"{Path(filename).stem}.sdp"
        output_path = OUTPUT_DIR / f"{upload_id}_{output_name}"
        try:
            run = convert_source_to_sdp(upload_path, output_path, options=options)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        report_path = REPORT_DIR / f"{upload_id}_{Path(filename).stem}.conversion.json"
        write_conversion_report(report_path, run.source_features, run.baseline_path, run.writer_outcome, options=run.options)
        token = _register_download(output_path, report_path)

        return jsonify({
            "downloadUrl": f"/api/builder/download/{token}",
            "outputFile": output_name,
            "converterBuild": converter_capabilities()["build"],
        })
    finally:
        pass


@app.post("/api/builder/export-pdf")
def builder_export_pdf():
    if not _REPORTLAB:
        return jsonify({"error": "PDF export not available — install reportlab."}), 503
    if ifcopenshell is None:
        return jsonify({"error": "IFC support unavailable — install ifcopenshell."}), 503

    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"error": "No file uploaded."}), 400

    filename = _sanitize_filename(upload.filename)
    if Path(filename).suffix.lower() != ".ifc":
        return jsonify({"error": "BisonBuilder requires a .ifc file."}), 400

    job_name = (request.form.get("jobName") or "").strip() or Path(filename).stem

    upload_id = uuid.uuid4().hex
    ifc_path = UPLOAD_DIR / f"{upload_id}_{filename}"
    upload.save(ifc_path)

    try:
        model = ifcopenshell.open(str(ifc_path))
        pdf_bytes = _generate_pdf(model, job_name)
        pdf_name = f"{Path(filename).stem}_construction.pdf"
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=pdf_name,
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        ifc_path.unlink(missing_ok=True)


@app.get("/api/builder/download/<token>")
def builder_download(token: str):
    with _downloads_lock:
        info = _download_registry.get(token)
    if not info:
        return jsonify({"error": "Download token not found."}), 404

    output_path = Path(info["output"])
    if not output_path.exists():
        return jsonify({"error": "Output file no longer exists."}), 404

    return send_file(output_path, as_attachment=True, download_name=output_path.name)


if __name__ == "__main__":
    port = int(os.environ.get("BUILDER_PORT", "5001"))
    app.run(host="127.0.0.1", port=port, debug=False)
