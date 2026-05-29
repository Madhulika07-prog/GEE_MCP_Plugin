// GEE chat-map frontend. Vanilla JS, Leaflet for the map.

const map = L.map("map", { center: [20.5, 78.5], zoom: 5, zoomControl: true });
L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png", {
  attribution: '© <a href="https://www.openstreetmap.org/copyright">OSM</a> contributors, © <a href="https://carto.com/">CARTO</a>',
  maxZoom: 19,
}).addTo(map);

// Layer registry: name -> { leaflet: L.Layer, meta: {...} }
const layers = new Map();
let sessionId = null;
let currentModel = null;

const $messages = document.getElementById("messages");
const $form = document.getElementById("chat-form");
const $input = document.getElementById("chat-input");
const $send = document.getElementById("send-btn");
const $status = document.getElementById("status-bar");
const $layersList = document.getElementById("layers-list");
const $resetBtn = document.getElementById("reset-btn");
const $modelSelect = document.getElementById("model-select");

function setStatus(text) { $status.textContent = text || ""; }

function pushMessage(role, text, opts = {}) {
  const div = document.createElement("div");
  div.className = `msg ${role}` + (opts.error ? " error" : "");
  if (opts.html) {
    div.innerHTML = text;
  } else if (opts.pre) {
    const pre = document.createElement("pre");
    pre.textContent = text;
    div.appendChild(pre);
  } else {
    text.split("\n").forEach((line, i) => {
      if (i > 0) div.appendChild(document.createElement("br"));
      div.appendChild(document.createTextNode(line));
    });
  }
  $messages.appendChild(div);
  $messages.scrollTop = $messages.scrollHeight;
  return div;
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

function refreshLayersPanel() {
  $layersList.innerHTML = "";
  if (layers.size === 0) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "No layers yet — ask the chat to add one.";
    $layersList.appendChild(li);
    return;
  }
  for (const [name, entry] of layers) {
    const li = document.createElement("li");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = map.hasLayer(entry.leaflet);
    cb.addEventListener("change", () => {
      if (cb.checked) entry.leaflet.addTo(map);
      else map.removeLayer(entry.leaflet);
    });
    const label = document.createElement("label");
    label.textContent = name;
    li.append(cb, label);
    if (entry.meta?.exportable) {
      const dl = document.createElement("button");
      dl.className = "export-btn";
      dl.textContent = "⬇";
      dl.title = "Export this layer";
      dl.addEventListener("click", () => toggleExportPanel(name));
      li.appendChild(dl);
    }
    const rm = document.createElement("button");
    rm.className = "remove";
    rm.textContent = "×";
    rm.title = "Remove";
    rm.addEventListener("click", () => removeLayer(name));
    li.appendChild(rm);
    $layersList.appendChild(li);
    if (entry.meta?.legend) {
      const leg = document.createElement("div");
      leg.className = "legend";
      leg.textContent = entry.meta.legend;
      $layersList.appendChild(leg);
    }
    if (entry.meta?.exportOpen) {
      $layersList.appendChild(renderExportPanel(name, entry));
    }
  }
}

function toggleExportPanel(name) {
  const entry = layers.get(name);
  if (!entry) return;
  entry.meta.exportOpen = !entry.meta.exportOpen;
  refreshLayersPanel();
}

function renderExportPanel(name, entry) {
  const meta = entry.meta.exportable;
  const wrap = document.createElement("div");
  wrap.className = "export-panel";
  const safeBase = name.replace(/[^A-Za-z0-9_\-]+/g, "_");
  wrap.innerHTML = `
    <label>Format
      <select class="ex-format">
        <option value="drive">Drive task (any size)</option>
        <option value="geotiff_url">Direct GeoTIFF download (~32 MB cap)</option>
      </select>
    </label>
    <div class="row">
      <label style="flex:1">Scale (m)
        <input class="ex-scale" type="number" value="30" min="1">
      </label>
      <label style="flex:2">File name
        <input class="ex-name" type="text" value="${safeBase}">
      </label>
    </div>
    <label class="ex-folder-wrap">Drive folder
      <input class="ex-folder" type="text" value="EarthEngine">
    </label>
    <button type="button" class="ex-go">Export</button>
  `;
  const fmtSel = wrap.querySelector(".ex-format");
  const folderWrap = wrap.querySelector(".ex-folder-wrap");
  fmtSel.addEventListener("change", () => {
    folderWrap.style.display = fmtSel.value === "drive" ? "block" : "none";
  });
  wrap.querySelector(".ex-go").addEventListener("click", async () => {
    const payload = {
      format: fmtSel.value,
      meta,
      scale: Number(wrap.querySelector(".ex-scale").value) || 30,
      file_name: wrap.querySelector(".ex-name").value || safeBase,
      folder: wrap.querySelector(".ex-folder").value || "EarthEngine",
    };
    setStatus("Submitting export…");
    try {
      const resp = await fetch("/export-layer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      if (!resp.ok) {
        pushMessage("assistant", `Export error: ${data.detail || JSON.stringify(data)}`, { error: true });
        return;
      }
      if (payload.format === "drive") {
        applyOperation({ op: "export_started", task: data });
      } else {
        applyOperation({ op: "download_url", url: data.download_url, note: data.note });
      }
    } catch (e) {
      pushMessage("assistant", `Network error during export: ${e.message}`, { error: true });
    } finally {
      setStatus("");
    }
  });
  return wrap;
}

function removeLayer(name) {
  const entry = layers.get(name);
  if (!entry) return;
  map.removeLayer(entry.leaflet);
  layers.delete(name);
  refreshLayersPanel();
}

function clearAllLayers() {
  for (const [, entry] of layers) map.removeLayer(entry.leaflet);
  layers.clear();
  refreshLayersPanel();
}

function uniqueName(base) {
  if (!layers.has(base)) return base;
  let n = 2;
  while (layers.has(`${base} (${n})`)) n++;
  return `${base} (${n})`;
}

function applyOperation(op) {
  if (op.op === "add_layer") {
    const { layer } = op;
    const name = uniqueName(layer.name);
    const tile = L.tileLayer(layer.tile_url, { opacity: 0.85, maxZoom: 19, attribution: "EE" });
    tile.addTo(map);
    const legendParts = [];
    if (layer.vis?.label) legendParts.push(layer.vis.label);
    if (typeof layer.vis?.min === "number" && typeof layer.vis?.max === "number") {
      legendParts.push(`${layer.vis.min} → ${layer.vis.max}`);
    }
    // If the layer came from add_ee_layer (it carries source/visualize/aoi_input
    // in meta), stash an `exportable` payload so the ⬇ button knows the params
    // it should re-run on the backend.
    const m = layer.meta || {};
    const exportable = (m.source && m.visualize && m.aoi_input && m.year)
      ? { source: m.source, visualize: m.visualize, aoi_input: m.aoi_input, year: m.year, months: m.months }
      : null;
    layers.set(name, {
      leaflet: tile,
      meta: { kind: "raster", legend: legendParts.join(" · "), exportable, exportOpen: false },
    });
    if (layer.bounds) map.fitBounds(layer.bounds, { maxZoom: 11 });
    refreshLayersPanel();

  } else if (op.op === "add_outline") {
    const { outline } = op;
    const name = uniqueName(outline.name);
    const color = outline.meta?.color || "#dc2626";
    const gj = L.geoJSON(outline.geojson, {
      style: { color, weight: 2, fill: outline.meta?.asset_type === "TABLE", fillOpacity: 0.1 },
    });
    gj.addTo(map);
    layers.set(name, { leaflet: gj, meta: { kind: "vector" } });
    if (outline.bounds) map.fitBounds(outline.bounds, { maxZoom: 11 });
    refreshLayersPanel();

  } else if (op.op === "clear_layers") {
    clearAllLayers();

  } else if (op.op === "show_stats") {
    pushMessage("assistant", JSON.stringify(op.stats, null, 2), { pre: true });

  } else if (op.op === "show_assets") {
    const { assets, asset_count, parent } = op.assets;
    if (!assets || assets.length === 0) {
      pushMessage("assistant", `No assets under ${escapeHTML(parent)}.`, { html: true });
    } else {
      const items = assets.slice(0, 50).map(a =>
        `<li><code>${escapeHTML(a.id || "")}</code> &mdash; ${escapeHTML(a.type || "?")}</li>`
      ).join("");
      const more = assets.length > 50 ? `<div style="font-size:11px;color:#666;">…and ${assets.length - 50} more</div>` : "";
      pushMessage("assistant",
        `<strong>${asset_count} asset${asset_count === 1 ? "" : "s"} under</strong> <code>${escapeHTML(parent)}</code>:<ul class="assets">${items}</ul>${more}`,
        { html: true });
    }

  } else if (op.op === "export_started") {
    const t = op.task;
    pushMessage("assistant",
      `<span class="badge export">Export started</span>Task <code>${escapeHTML(t.task_id)}</code> &mdash; <strong>${escapeHTML(t.description)}</strong> → Drive folder <code>${escapeHTML(t.folder)}</code>. Track at <a href="https://code.earthengine.google.com/tasks" target="_blank">EE Tasks</a>.`,
      { html: true });

  } else if (op.op === "download_url") {
    pushMessage("assistant",
      `<span class="badge download">Download ready</span><a href="${op.url}" target="_blank">Click to download GeoTIFF</a><div style="font-size:11px;color:#666;">${escapeHTML(op.note || "")}</div>`,
      { html: true });

  } else if (op.op === "show_tasks") {
    const ts = op.tasks || [];
    if (ts.length === 0) {
      pushMessage("assistant", "No recent export tasks.");
    } else {
      const rows = ts.map(t =>
        `<li><code>${escapeHTML(t.id)}</code> &mdash; <strong>${escapeHTML(t.state)}</strong> &mdash; ${escapeHTML(t.description || "")}</li>`
      ).join("");
      pushMessage("assistant", `<strong>Recent export tasks:</strong><ul class="assets">${rows}</ul>`, { html: true });
    }

  } else {
    console.warn("unknown op", op);
  }
}

async function sendMessage(text) {
  pushMessage("user", text);
  $send.disabled = true;
  setStatus(`Thinking via ${currentModel}…`);
  try {
    const resp = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: sessionId, model_id: currentModel }),
    });
    if (!resp.ok) {
      const err = await resp.text();
      pushMessage("assistant", `Backend error (${resp.status}): ${err}`, { error: true });
      return;
    }
    const data = await resp.json();
    sessionId = data.session_id;
    if (data.model_id) currentModel = data.model_id;
    for (const op of data.operations || []) applyOperation(op);
    if (data.text) pushMessage("assistant", data.text);
  } catch (e) {
    pushMessage("assistant", `Network error: ${e.message}`, { error: true });
  } finally {
    $send.disabled = false;
    setStatus("");
    $input.focus();
  }
}

async function loadModels() {
  const { models, default: defaultModel } = await fetch("/models").then(r => r.json());
  currentModel = currentModel || defaultModel;
  $modelSelect.innerHTML = "";
  const groups = {};
  for (const m of models) (groups[m.provider] ||= []).push(m);
  for (const [provider, ms] of Object.entries(groups)) {
    const og = document.createElement("optgroup");
    og.label = provider;
    for (const m of ms) {
      const o = document.createElement("option");
      o.value = m.model_id;
      const tag = m.free ? "🆓" : "💳";
      o.textContent = `${tag} ${m.display}${m.available ? "" : " (no key)"}`;
      o.disabled = !m.available;
      if (!m.available) o.title = `Set ${m.key_env} in gee-mcp\\.env`;
      og.appendChild(o);
    }
    $modelSelect.appendChild(og);
  }
  $modelSelect.value = currentModel;
}

// ---------- upload modal ----------
const $uploadBtn = document.getElementById("upload-btn");
const $uploadModal = document.getElementById("upload-modal");
const $uploadForm = document.getElementById("upload-form");
const $uploadFile = document.getElementById("upload-file");
const $uploadName = document.getElementById("upload-name");
const $uploadDesc = document.getElementById("upload-desc");
const $uploadSubmit = document.getElementById("upload-submit");

function openUploadModal() { $uploadModal.classList.remove("hidden"); }
function closeUploadModal() {
  $uploadModal.classList.add("hidden");
  $uploadForm.reset();
}
$uploadBtn.addEventListener("click", openUploadModal);
for (const btn of $uploadModal.querySelectorAll(".close-modal")) {
  btn.addEventListener("click", closeUploadModal);
}
$uploadModal.addEventListener("click", (e) => {
  if (e.target === $uploadModal) closeUploadModal();
});
$uploadForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = $uploadFile.files[0];
  const name = $uploadName.value.trim();
  if (!file || !name) return;
  const fd = new FormData();
  fd.append("file", file);
  fd.append("asset_name", name);
  fd.append("description", $uploadDesc.value || "");
  $uploadSubmit.disabled = true;
  $uploadSubmit.textContent = "Uploading…";
  try {
    const resp = await fetch("/upload-asset", { method: "POST", body: fd });
    const data = await resp.json();
    if (!resp.ok) {
      pushMessage("assistant", `Upload error: ${data.detail || JSON.stringify(data)}`, { error: true });
    } else {
      const fmt = data.input_format === "shapefile_zip" ? "Shapefile" : "GeoJSON";
      const featCount = data.feature_count ?? "?";
      const skipNote = data.skipped_geometries
        ? ` (${data.skipped_geometries} empty/null geometries skipped)`
        : "";
      const leaf = data.asset_id.split("/").pop();
      pushMessage("assistant",
        `<span class="badge export">Uploaded</span>${fmt} with ${featCount} features${skipNote}. ` +
        `<strong>You can now use <code>${escapeHTML(leaf)}</code> as an AOI name in chat right away</strong> — ` +
        `e.g. <em>"Show LST over ${escapeHTML(leaf)} for May 2024"</em>. ` +
        `<br><small>EE asset ingestion task <code>${escapeHTML(data.task_id)}</code> is also running — once DONE in <a href="https://code.earthengine.google.com/tasks" target="_blank">EE Tasks</a>, the asset will be at <code>${escapeHTML(data.asset_id)}</code>.</small>`,
        { html: true });
      closeUploadModal();
    }
  } catch (err) {
    pushMessage("assistant", `Network error during upload: ${err.message}`, { error: true });
  } finally {
    $uploadSubmit.disabled = false;
    $uploadSubmit.textContent = "Upload";
  }
});

$modelSelect.addEventListener("change", () => {
  currentModel = $modelSelect.value;
  pushMessage("system", `Switched to ${$modelSelect.options[$modelSelect.selectedIndex].text.trim()}`);
});

$form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $input.value.trim();
  if (!text) return;
  $input.value = "";
  sendMessage(text);
});

$input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $form.requestSubmit();
  }
});

$resetBtn.addEventListener("click", async () => {
  if (!confirm("Clear conversation and remove all map layers?")) return;
  if (sessionId) {
    await fetch("/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }),
    });
  }
  sessionId = null;
  $messages.innerHTML = "";
  clearAllLayers();
  pushMessage("system", "Conversation reset.");
});

// Boot: load models, then check health.
(async () => {
  try { await loadModels(); } catch (e) { console.error("models load failed", e); }
  const h = await fetch("/health").then(r => r.json());
  const anyKey = Object.values(h.provider_keys_set || {}).some(Boolean);
  if (!anyKey) {
    pushMessage("assistant", "No LLM API key found. Add GEMINI_API_KEY (free at https://aistudio.google.com/apikey) or another provider key to gee-mcp\\.env and restart.", { error: true });
  } else if (!h.ee_ok) {
    pushMessage("assistant", `Earth Engine init failed: ${h.ee_error}`, { error: true });
  } else {
    const providers = Object.entries(h.provider_keys_set).filter(([, ok]) => ok).map(([p]) => p).join(", ");
    pushMessage("system", `Connected — providers available: ${providers}. Try "Show NDVI over Bangalore for May 2024" or "list my assets".`);
  }
  refreshLayersPanel();
})();
