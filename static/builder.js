import * as THREE from "https://esm.sh/three@0.164.1";
import { OrbitControls } from "https://esm.sh/three@0.164.1/examples/jsm/controls/OrbitControls.js";

const fileInput = document.getElementById("fileInput");
const loadBtn = document.getElementById("loadBtn");
const dropZone = document.getElementById("dropZone");
const chosenFile = document.getElementById("chosenFile");
const searchInput = document.getElementById("searchInput");
const jobNameInput = document.getElementById("jobNameInput");
const exportBtn = document.getElementById("exportBtn");
const statusText = document.getElementById("statusText");
const fileLabel = document.getElementById("fileLabel");
const statsLabel = document.getElementById("statsLabel");
const modalOverlay = document.getElementById("modalOverlay");
const modalMessage = document.getElementById("modalMessage");
const modalClose = document.getElementById("modalClose");
const viewerContainer = document.getElementById("viewerContainer");
const noSelection = document.getElementById("noSelection");
const componentInfo = document.getElementById("componentInfo");
const compName = document.getElementById("compName");
const compBucket = document.getElementById("compBucket");
const compW = document.getElementById("compW");
const compD = document.getElementById("compD");
const compH = document.getElementById("compH");
const statusCard = document.getElementById("statusCard");
const projectCard = document.getElementById("projectCard");
const projectSelect = document.getElementById("projectSelect");
const projectNameLabel = document.getElementById("projectNameLabel");
const assignedModelLabel = document.getElementById("assignedModelLabel");
const assignControls = document.getElementById("assignControls");
const assignedFileInput = document.getElementById("assignedFileInput");
const assignedUploadBtn = document.getElementById("assignedUploadBtn");
const clearAssignedBtn = document.getElementById("clearAssignedBtn");
const uploadCard = document.getElementById("uploadCard");

let scene;
let camera;
let renderer;
let controls;
let modelGroup = null;

// panelId → { meshes: Mesh[], info: panelInfo }
const panelMap = new Map();
// lowercase panel name → panelId  (for search)
const panelNameIndex = new Map();
let selectedPanelId = null;
let builderContext = null;
let loadedAssignedProjectId = null;
let loadedUploadFile = null;
let parentAccessToken = "";
let parentCapabilities = null;

const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
raycaster.params.Mesh = { threshold: 0 };

const M_TO_IN = 39.3701;

let _pointerDownAt = null;

const HIGHLIGHT_EMISSIVE = 0xffffff;
const HIGHLIGHT_INTENSITY = 0.22;

function getAccessToken() {
  if (parentAccessToken) {
    return parentAccessToken;
  }
  try {
    return sessionStorage.getItem("bw_token") || localStorage.getItem("bw_token") || "";
  } catch (_error) {
    return "";
  }
}

function normalizeParentCapabilities(value) {
  const source = value && typeof value === "object" ? value : {};
  const canAssign = !!source.canAssign;
  return {
    canUpload: !!source.canUpload,
    canAssign,
    canClearAssignment:
      typeof source.canClearAssignment === "boolean" ? source.canClearAssignment : canAssign,
    canUseProjectSelector: !!source.canUseProjectSelector || canAssign,
    assignedOnly: !!source.assignedOnly,
  };
}

function capabilitiesFromAuthMessage(data) {
  if (data?.capabilities && typeof data.capabilities === "object") {
    return normalizeParentCapabilities(data.capabilities);
  }
  if (typeof data?.canUpload === "boolean") {
    return normalizeParentCapabilities({
      canUpload: data.canUpload,
      assignedOnly: !data.canUpload,
    });
  }
  return parentCapabilities;
}

function getEffectiveCapabilities() {
  const projects = builderContext?.projects || [];
  const backendCanUpload = !!builderContext?.can_upload;
  const backendCanAssign = !!builderContext?.can_assign;

  if (!parentCapabilities) {
    return {
      canUpload: backendCanUpload,
      canAssign: backendCanAssign,
      canClearAssignment: backendCanAssign,
      canUseProjectSelector: backendCanAssign || (!builderContext?.assigned_only && projects.length > 0),
      assignedOnly: !!builderContext?.assigned_only,
    };
  }

  return {
    canUpload: !!parentCapabilities.canUpload && backendCanUpload,
    canAssign: !!parentCapabilities.canAssign && backendCanAssign,
    canClearAssignment: !!parentCapabilities.canClearAssignment && backendCanAssign,
    canUseProjectSelector:
      !!parentCapabilities.canUseProjectSelector && (backendCanAssign || projects.length > 0),
    assignedOnly: !!parentCapabilities.assignedOnly || !!builderContext?.assigned_only,
  };
}

function isTrustedAuthOrigin(origin) {
  const trusted = new Set([
    window.location.origin,
    "https://pipeline.scottsdaleutah.com",
    "https://www.pipeline.scottsdaleutah.com",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
  ]);
  return trusted.has(origin);
}

function handleAuthMessage(event) {
  if (!isTrustedAuthOrigin(event.origin)) {
    return;
  }
  const data = event.data || {};
  if (data.type !== "bisonworks-builder-auth") {
    return;
  }
  const nextToken = String(data.accessToken || "").trim();
  parentCapabilities = capabilitiesFromAuthMessage(data);
  if (nextToken === parentAccessToken) {
    renderProjectContext();
    return;
  }
  parentAccessToken = nextToken;
  if (parentAccessToken) {
    loadBuilderContext();
  }
}

function notifyParentReady() {
  if (window.parent && window.parent !== window) {
    window.parent.postMessage({ type: "bisonbuilder-ready" }, "*");
  }
}

function builderApiBase() {
  const explicitBase = String(window.BISON_BUILDER_API_BASE || "").trim();
  if (explicitBase) {
    return explicitBase.replace(/\/+$/, "");
  }

  const host = window.location.hostname.toLowerCase();
  if (
    host === "builder.scottsdaleutah.com" ||
    host === "pipeline.scottsdaleutah.com" ||
    host.endsWith(".github.io")
  ) {
    return "https://api.scottsdaleutah.com";
  }

  return "";
}

function builderApiPath(path) {
  const base = builderApiBase();
  return `${base}${path}`;
}

function authHeaders(extra = {}) {
  const token = getAccessToken();
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra;
}

async function builderFetch(path, options = {}) {
  const headers = authHeaders(options.headers || {});
  return fetch(builderApiPath(path), { ...options, headers });
}

async function parseJsonResponse(response, fallbackMessage = "Request failed.") {
  let payload = null;
  try {
    payload = await response.json();
  } catch (_error) {
    payload = null;
  }
  if (!response.ok) {
    throw new Error(payload?.detail || payload?.error || fallbackMessage);
  }
  return payload;
}

function projectLabel(project) {
  const number = String(project?.project_number || "").trim();
  const name = String(project?.name || "").trim();
  return number && name ? `${number} - ${name}` : name || number || project?.id || "Unnamed project";
}

function getSelectedProject() {
  const projectId = projectSelect?.value || "";
  return (builderContext?.projects || []).find((project) => project.id === projectId) || null;
}

function setStatus(message, isError = false) {
  statusText.textContent = message;
  statusText.style.color = isError ? "#e2646d" : "#e8edf2";
  if (isError) statusCard.classList.remove("hidden");
}

function initViewer() {
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0f1a25);

  camera = new THREE.PerspectiveCamera(
    58,
    viewerContainer.clientWidth / Math.max(viewerContainer.clientHeight, 1),
    0.1,
    100000
  );
  camera.position.set(12, 12, 12);

  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setSize(viewerContainer.clientWidth, viewerContainer.clientHeight);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  viewerContainer.appendChild(renderer.domElement);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.target.set(0, 0, 0);

  scene.add(new THREE.HemisphereLight(0xd4ebff, 0x1c2530, 0.95));
  const dir = new THREE.DirectionalLight(0xffffff, 1.0);
  dir.position.set(10, 16, 8);
  scene.add(dir);

  scene.add(new THREE.GridHelper(200, 80, 0x2d5570, 0x1f3342));
  scene.add(new THREE.AxesHelper(3));

  renderer.domElement.addEventListener("pointerdown", onCanvasPointerDown);
  renderer.domElement.addEventListener("pointerup", onCanvasPointerUp);
  animate();
}

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}

function fitCameraToObject(object) {
  const bbox = new THREE.Box3().setFromObject(object);
  if (bbox.isEmpty()) return;
  const center = bbox.getCenter(new THREE.Vector3());
  const size = bbox.getSize(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z, 1);
  const dist = maxDim * 1.4;
  camera.position.set(center.x + dist, center.y + dist * 0.7, center.z + dist);
  camera.near = maxDim / 500;
  camera.far = maxDim * 200;
  camera.updateProjectionMatrix();
  controls.target.copy(center);
  controls.update();
}

function base64ToBuffer(base64) {
  const binary = atob(base64 || "");
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

function decodeFloat32(b64) { return new Float32Array(base64ToBuffer(b64)); }
function decodeUint32(b64)  { return new Uint32Array(base64ToBuffer(b64)); }

// IFC is Z-up; Three.js is Y-up.
function remapZUp(positions) {
  const out = new Float32Array(positions.length);
  for (let i = 0; i < positions.length; i += 3) {
    out[i]     = positions[i];
    out[i + 1] = positions[i + 2];
    out[i + 2] = -positions[i + 1];
  }
  return out;
}

function clearModel() {
  if (!modelGroup) return;
  scene.remove(modelGroup);
  modelGroup.traverse((child) => {
    if (child.geometry) child.geometry.dispose();
    if (Array.isArray(child.material)) child.material.forEach((m) => m.dispose());
    else child.material?.dispose();
  });
  modelGroup = null;
  panelMap.clear();
  panelNameIndex.clear();
  selectedPanelId = null;
}

function fmtIn(meters) {
  if (meters == null) return "—";
  return (Number(meters) * M_TO_IN).toFixed(2);
}

function setPanelHighlight(panelId, highlighted) {
  const entry = panelMap.get(panelId);
  if (!entry) return;
  for (const mesh of entry.meshes) {
    mesh.material.emissive.setHex(highlighted ? HIGHLIGHT_EMISSIVE : 0x000000);
    mesh.material.emissiveIntensity = highlighted ? HIGHLIGHT_INTENSITY : 0;
    mesh.material.opacity = highlighted ? 1.0 : 0.94;
  }
}

function showPanelInfo(panelId) {
  const entry = panelMap.get(panelId);
  if (!entry) return;

  // Compute combined bounding box across all meshes in the panel
  const bbox = new THREE.Box3();
  for (const mesh of entry.meshes) {
    bbox.expandByObject(mesh);
  }
  const center = bbox.getCenter(new THREE.Vector3());
  const size = bbox.getSize(new THREE.Vector3());

  const { info } = entry;
  compName.textContent   = info.panelName || "—";
  compBucket.textContent = info.bucket    || "—";
  compW.textContent = fmtIn(size.x);
  compD.textContent = fmtIn(size.z);
  compH.textContent = fmtIn(size.y);

  noSelection.classList.add("hidden");
  componentInfo.classList.remove("hidden");
}

function clearSelection() {
  if (selectedPanelId !== null) {
    setPanelHighlight(selectedPanelId, false);
    selectedPanelId = null;
  }
  noSelection.classList.remove("hidden");
  componentInfo.classList.add("hidden");
}

function selectPanel(panelId) {
  if (selectedPanelId !== null && selectedPanelId !== panelId) {
    setPanelHighlight(selectedPanelId, false);
  }
  selectedPanelId = panelId;
  setPanelHighlight(panelId, true);
  showPanelInfo(panelId);
}

function onCanvasPointerDown(event) {
  _pointerDownAt = { x: event.clientX, y: event.clientY };
}

function onCanvasPointerUp(event) {
  if (!_pointerDownAt) return;
  const dx = event.clientX - _pointerDownAt.x;
  const dy = event.clientY - _pointerDownAt.y;
  _pointerDownAt = null;
  if (Math.sqrt(dx * dx + dy * dy) > 4) return;

  if (!modelGroup) {
    setStatus("No model loaded yet.", true);
    return;
  }

  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x =  ((event.clientX - rect.left) / rect.width)  * 2 - 1;
  pointer.y = -((event.clientY - rect.top)  / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);

  const meshes = [];
  modelGroup.traverse((child) => { if (child.isMesh) meshes.push(child); });

  const hits = raycaster.intersectObjects(meshes, false);
  if (hits.length > 0) {
    const panelId = hits[0].object.userData.panelId;
    if (panelId != null) {
      selectPanel(panelId);
    } else {
      setStatus(`Hit mesh but panelId is missing (${typeof panelId}).`, true);
    }
  } else {
    setStatus(`No geometry hit — ${meshes.length} meshes in scene, canvas ${Math.round(rect.width)}×${Math.round(rect.height)}.`, true);
    clearSelection();
  }
}

function buildModel(payload) {
  clearModel();

  const group = new THREE.Group();

  for (const entity of payload.entities || []) {
    const positions = remapZUp(decodeFloat32(entity.positionsB64));
    const indices   = decodeUint32(entity.indicesB64);
    if (!positions.length || !indices.length) continue;

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.setIndex(new THREE.BufferAttribute(indices, 1));
    geometry.computeVertexNormals();
    geometry.computeBoundingBox();
    geometry.computeBoundingSphere();

    const material = new THREE.MeshStandardMaterial({
      color: entity.color || 0x9aa6b2,
      roughness: 0.85,
      metalness: 0.08,
      transparent: true,
      opacity: 0.94,
      side: THREE.DoubleSide,
    });

    const mesh = new THREE.Mesh(geometry, material);
    mesh.userData.panelId = entity.panelId;

    // Register mesh under its panel group
    if (!panelMap.has(entity.panelId)) {
      panelMap.set(entity.panelId, {
        meshes: [],
        info: {
          panelId:       entity.panelId,
          panelGlobalId: entity.panelGlobalId,
          panelName:     entity.panelName,
          panelType:     entity.panelType,
          bucket:        entity.bucket,
        },
      });
    }
    panelMap.get(entity.panelId).meshes.push(mesh);

    group.add(mesh);
  }

  // Build name → panelId lookup for search
  for (const [panelId, entry] of panelMap) {
    const key = (entry.info.panelName || "").toLowerCase().trim();
    if (key && !panelNameIndex.has(key)) panelNameIndex.set(key, panelId);
  }

  modelGroup = group;
  scene.add(group);
  fitCameraToObject(group);
}

function fitCameraToBbox(bbox) {
  if (bbox.isEmpty()) return;
  const center = bbox.getCenter(new THREE.Vector3());
  const size = bbox.getSize(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z, 1);
  const dist = maxDim * 3;
  camera.position.set(center.x + dist, center.y + dist * 0.7, center.z + dist);
  camera.near = maxDim / 500;
  camera.far = maxDim * 200;
  camera.updateProjectionMatrix();
  controls.target.copy(center);
  controls.update();
}

function showModal(message) {
  modalMessage.textContent = message;
  modalOverlay.classList.remove("hidden");
}

function hideModal() {
  modalOverlay.classList.add("hidden");
  searchInput.focus();
}

function searchPanel() {
  const query = searchInput.value.trim();
  if (!query) return;

  const panelId = panelNameIndex.get(query.toLowerCase());
  if (panelId != null) {
    selectPanel(panelId);
    const entry = panelMap.get(panelId);
    if (entry) {
      const bbox = new THREE.Box3();
      for (const mesh of entry.meshes) bbox.expandByObject(mesh);
      fitCameraToBbox(bbox);
    }
  } else {
    showModal(`No panel named "${query}" exists in this model. Please check the name and try again.`);
  }
}

async function loadModel() {
  if (!getEffectiveCapabilities().canUpload) {
    setStatus("This role can only view assigned Builder models.", true);
    return;
  }

  const file = fileInput.files?.[0];
  if (!file) {
    setStatus("Choose an .ifc file first.", true);
    return;
  }
  if (!file.name.toLowerCase().endsWith(".ifc")) {
    setStatus("BisonBuilder only supports .ifc files.", true);
    return;
  }

  fileLabel.textContent = file.name;
  statsLabel.textContent = "";
  setStatus(`Loading ${file.name}...`);
  loadBtn.disabled = true;

  try {
    const params = new URLSearchParams({ filename: file.name });
    if (file.type) params.set("content_type", file.type);
    const headers = file.type ? { "Content-Type": file.type } : {};
    const response = await builderFetch(`/builder/preview?${params.toString()}`, {
      method: "POST",
      headers,
      body: file,
    });
    const payload = await parseJsonResponse(response, "Load failed.");

    buildModel(payload);
    loadedUploadFile = file;
    loadedAssignedProjectId = null;
    const panelCount = panelMap.size;
    const skipped = payload.skipped ? ` · ${payload.skipped} skipped` : "";
    statsLabel.textContent = `${panelCount} panels · ${payload.entityCount} members${skipped}`;
    setStatus("Model loaded. Click any component to inspect the full panel.");
    statusCard.classList.add("hidden");
  } catch (error) {
    console.error(error);
    setStatus(`Load failed: ${error.message}`, true);
  } finally {
    loadBtn.disabled = false;
  }
}

function renderProjectContext(preferredProjectId = "") {
  if (!builderContext) return;
  const projects = builderContext.projects || [];
  const capabilities = getEffectiveCapabilities();
  const showUploadCard = capabilities.canUpload;
  const showAssignmentControls = capabilities.canAssign || capabilities.canClearAssignment;
  const showProjectCard = projects.length > 0 || showAssignmentControls || capabilities.canUseProjectSelector;
  if (uploadCard) uploadCard.classList.toggle("hidden", !showUploadCard);
  if (projectCard) projectCard.classList.toggle("hidden", !showProjectCard);
  if (assignControls) assignControls.classList.toggle("hidden", !showAssignmentControls);
  if (assignedFileInput) {
    assignedFileInput.classList.add("hidden");
    assignedFileInput.disabled = !capabilities.canAssign;
  }
  if (assignedUploadBtn) assignedUploadBtn.classList.toggle("hidden", !capabilities.canAssign);
  if (clearAssignedBtn) clearAssignedBtn.classList.toggle("hidden", !capabilities.canClearAssignment);
  if (!projectSelect) return;

  if (projectNameLabel) projectNameLabel.classList.toggle("hidden", !capabilities.assignedOnly);
  projectSelect.classList.toggle("hidden", capabilities.assignedOnly);
  projectSelect.disabled = !capabilities.canUseProjectSelector;
  projectSelect.innerHTML = "";
  for (const project of projects) {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = `${projectLabel(project)}${project.has_builder_model ? "" : " (no model)"}`;
    projectSelect.appendChild(option);
  }

  const selected =
    projects.find((project) => project.id === preferredProjectId) ||
    projects.find((project) => project.has_builder_model) ||
    projects[0] ||
    null;

  if (!selected) {
    if (projectNameLabel) projectNameLabel.textContent = "No project assigned.";
    assignedModelLabel.textContent = capabilities.canAssign
      ? "Select a project to assign a Builder model."
      : "No Builder model assigned yet.";
    if (showUploadCard) {
      setStatus("Drop or choose an IFC file to load a model.");
    } else {
      setStatus("No Builder model is assigned to this account.", true);
    }
    return;
  }

  projectSelect.value = selected.id;
  if (projectNameLabel) projectNameLabel.textContent = projectLabel(selected);
  assignedModelLabel.textContent = selected.has_builder_model
    ? `Assigned: ${selected.builder_file_name}`
    : "No Builder model assigned.";
}

async function loadBuilderContext() {
  try {
    const response = await builderFetch("/builder/context");
    builderContext = await parseJsonResponse(response, "Unable to load Builder context.");
    renderProjectContext();
    const capabilities = getEffectiveCapabilities();
    const selected = getSelectedProject();
    if (selected?.has_builder_model) {
      await loadAssignedProjectModel(selected.id);
    } else if (capabilities.assignedOnly || !capabilities.canUpload) {
      clearModel();
      fileLabel.textContent = "No model assigned";
      statsLabel.textContent = "";
      setStatus("No Builder model has been assigned yet.", true);
    }
  } catch (error) {
    console.error(error);
    if (uploadCard) uploadCard.classList.add("hidden");
    setStatus(`Builder setup failed: ${error.message}`, true);
  }
}

async function loadAssignedProjectModel(projectId) {
  const project = (builderContext?.projects || []).find((item) => item.id === projectId);
  if (!project?.has_builder_model) {
    clearModel();
    loadedAssignedProjectId = null;
    fileLabel.textContent = "No model assigned";
    statsLabel.textContent = "";
    setStatus("No Builder model is assigned to this project.", true);
    return;
  }

  fileLabel.textContent = project.builder_file_name || "Assigned Builder model";
  statsLabel.textContent = "";
  setStatus(`Loading ${project.builder_file_name || "assigned model"}...`);
  try {
    const response = await builderFetch(`/builder/projects/${encodeURIComponent(projectId)}/model/preview`);
    const payload = await parseJsonResponse(response, "Assigned model load failed.");
    buildModel(payload);
    loadedAssignedProjectId = projectId;
    loadedUploadFile = null;
    const panelCount = panelMap.size;
    const skipped = payload.skipped ? ` Â· ${payload.skipped} skipped` : "";
    statsLabel.textContent = `${panelCount} panels Â· ${payload.entityCount} members${skipped}`;
    setStatus("Assigned model loaded. Click any component to inspect the full panel.");
    statusCard.classList.add("hidden");
  } catch (error) {
    console.error(error);
    setStatus(`Assigned model load failed: ${error.message}`, true);
  }
}

async function uploadAssignedModel() {
  if (!getEffectiveCapabilities().canAssign) {
    setStatus("Your role cannot assign Builder models.", true);
    return;
  }

  const project = getSelectedProject();
  const file = assignedFileInput?.files?.[0];
  if (!project) {
    setStatus("Select a project first.", true);
    return;
  }
  if (!file) {
    assignedFileInput?.click();
    return;
  }
  if (!file.name.toLowerCase().endsWith(".ifc")) {
    setStatus("BisonBuilder only supports .ifc files.", true);
    return;
  }

  assignedUploadBtn.disabled = true;
  setStatus(`Assigning ${file.name}...`);
  try {
    const params = new URLSearchParams({ filename: file.name });
    if (file.type) params.set("content_type", file.type);
    const headers = file.type ? { "Content-Type": file.type } : {};
    const response = await builderFetch(`/builder/projects/${encodeURIComponent(project.id)}/model/upload?${params.toString()}`, {
      method: "POST",
      headers,
      body: file,
    });
    const updated = await parseJsonResponse(response, "Assignment failed.");
    builderContext.projects = (builderContext.projects || []).map((item) =>
      item.id === updated.id ? updated : item
    );
    renderProjectContext(updated.id);
    assignedFileInput.value = "";
    await loadAssignedProjectModel(updated.id);
  } catch (error) {
    console.error(error);
    setStatus(`Assignment failed: ${error.message}`, true);
  } finally {
    assignedUploadBtn.disabled = false;
  }
}

function chooseAssignedModelFile() {
  if (!getEffectiveCapabilities().canAssign) {
    setStatus("Your role cannot assign Builder models.", true);
    return;
  }
  if (!getSelectedProject()) {
    setStatus("Select a project first.", true);
    return;
  }
  if (assignedFileInput) {
    assignedFileInput.value = "";
    assignedFileInput.click();
  }
}

async function clearAssignedModel() {
  if (!getEffectiveCapabilities().canClearAssignment) {
    setStatus("Your role cannot clear Builder model assignments.", true);
    return;
  }

  const project = getSelectedProject();
  if (!project) {
    setStatus("Select a project first.", true);
    return;
  }
  clearAssignedBtn.disabled = true;
  setStatus("Clearing Builder model assignment...");
  try {
    const response = await builderFetch(`/builder/projects/${encodeURIComponent(project.id)}/model`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_id: null }),
    });
    const updated = await parseJsonResponse(response, "Unable to clear assignment.");
    builderContext.projects = (builderContext.projects || []).map((item) =>
      item.id === updated.id ? updated : item
    );
    renderProjectContext(updated.id);
    clearModel();
    loadedAssignedProjectId = null;
    fileLabel.textContent = "No model assigned";
    statsLabel.textContent = "";
    setStatus("Builder model assignment cleared.");
  } catch (error) {
    console.error(error);
    setStatus(`Unable to clear assignment: ${error.message}`, true);
  } finally {
    clearAssignedBtn.disabled = false;
  }
}

function onResize() {
  const w = viewerContainer.clientWidth;
  const h = viewerContainer.clientHeight;
  if (!w || !h) return;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
}

function setChosenFile(name) {
  chosenFile.textContent = name;
  chosenFile.classList.remove("hidden");
}

fileInput.addEventListener("change", () => {
  if (fileInput.files?.[0]) setChosenFile(fileInput.files[0].name);
});

dropZone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropZone.classList.add("drag-over");
});

dropZone.addEventListener("dragleave", () => {
  dropZone.classList.remove("drag-over");
});

dropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropZone.classList.remove("drag-over");
  const file = e.dataTransfer.files[0];
  if (!file) return;
  const dt = new DataTransfer();
  dt.items.add(file);
  fileInput.files = dt.files;
  setChosenFile(file.name);
});

searchInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") searchPanel();
});

modalClose.addEventListener("click", hideModal);
modalOverlay.addEventListener("click", (e) => {
  if (e.target === modalOverlay) hideModal();
});

async function exportPdf() {
  const project = loadedAssignedProjectId
    ? (builderContext?.projects || []).find((item) => item.id === loadedAssignedProjectId)
    : null;
  const file = loadedUploadFile || fileInput.files?.[0];
  if (!project?.has_builder_model && !file) {
    setStatus("Load an IFC file first.", true);
    return;
  }
  const sourceName = project?.builder_file_name || file?.name || "builder_model.ifc";
  const jobName = jobNameInput.value.trim() || sourceName.replace(/\.ifc$/i, "");
  exportBtn.disabled = true;
  setStatus("Generating PDF...");
  try {
    let response;
    if (project?.has_builder_model) {
      const params = new URLSearchParams({ job_name: jobName });
      response = await builderFetch(
        `/builder/projects/${encodeURIComponent(project.id)}/model/export-pdf?${params.toString()}`
      );
    } else {
      const params = new URLSearchParams({ filename: file.name, job_name: jobName });
      const headers = file.type ? { "Content-Type": file.type } : {};
      response = await builderFetch(`/builder/export-pdf?${params.toString()}`, {
        method: "POST",
        headers,
        body: file,
      });
    }
    if (!response.ok) {
      const err = await response.json().catch(() => null);
      throw new Error(err?.detail || err?.error || "Export failed.");
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = sourceName.replace(/\.ifc$/i, "") + "_construction.pdf";
    a.click();
    URL.revokeObjectURL(url);
    setStatus("PDF exported.");
  } catch (error) {
    console.error(error);
    setStatus(`Export failed: ${error.message}`, true);
  } finally {
    exportBtn.disabled = false;
  }
}

loadBtn.addEventListener("click", loadModel);
exportBtn.addEventListener("click", exportPdf);
projectSelect?.addEventListener("change", () => {
  if (!getEffectiveCapabilities().canUseProjectSelector) {
    renderProjectContext(projectSelect.value);
    return;
  }
  loadAssignedProjectModel(projectSelect.value);
});
assignedFileInput?.addEventListener("change", uploadAssignedModel);
assignedUploadBtn?.addEventListener("click", chooseAssignedModelFile);
clearAssignedBtn?.addEventListener("click", clearAssignedModel);
window.addEventListener("message", handleAuthMessage);
window.addEventListener("resize", onResize);

initViewer();
notifyParentReady();
loadBuilderContext();
