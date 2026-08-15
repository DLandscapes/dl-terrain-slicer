"use strict";

// must match APP_BUILD in app/core.py and the ?v= tags in index.html
const EXPECTED_BUILD = 27;

/* Can this browser run the engine at all?
 *
 * Checked BEFORE booting, because the failure was silent: an older browser got
 * a dead page and no explanation. The gate is the two things Pyodide actually
 * needs that older browsers lack - WebAssembly with BigInt (i64 <-> JS), and
 * MODULE workers, which Pyodide requires and which Firefox only shipped in
 * 114 (June 2023). Chrome/Edge 80+, Safari 15+.
 *
 * Feature-detected, never sniffed from the user agent: version strings lie,
 * and a capability test stays correct for browsers that do not exist yet. */
function browserBlocker() {
  if (typeof WebAssembly === "undefined") return "This browser has no WebAssembly.";
  try {
    new WebAssembly.Global({ value: "i64", mutable: true }, 0n);
  } catch {
    return "This browser's WebAssembly is too old (no 64-bit integer support).";
  }
  try {
    const url = URL.createObjectURL(new Blob([""], { type: "text/javascript" }));
    new Worker(url, { type: "module" }).terminate();
    URL.revokeObjectURL(url);
  } catch {
    return "This browser cannot run module workers, which the Python engine needs. "
      + "Firefox added them in version 114.";
  }
  return null;
}

if (api.mode === "wasm") {
  const why = browserBlocker();
  if (why) {
    document.querySelector("#unsupported-why").textContent = why;
    document.querySelector("#unsupported").hidden = false;
    document.querySelector("#boot").hidden = true;
    throw new Error("unsupported browser: " + why);  // stop the rest of app.js
  }
}

/* Boot: in wasm mode nothing works until Pyodide is up, so show progress and
 * keep the UI disabled meanwhile. In server mode ready() resolves at once. */
(async () => {
  const boot = document.querySelector("#boot");
  const fill = document.querySelector("#boot-fill");
  const stepEl = document.querySelector("#boot-step");
  const STEPS = ["runtime", "packages", "slicer", "ready"];
  if (api.mode === "wasm") {
    boot.hidden = false;
    api.onProgress = (step, detail) => {
      const i = Math.max(0, STEPS.indexOf(step));
      fill.style.width = `${((i + 1) / STEPS.length) * 100}%`;
      stepEl.textContent = detail || step;
    };
  }
  let stale = false;
  try {
    await api.ready();
    stale = (await api.version()).build !== EXPECTED_BUILD;
  } catch (err) {
    if (api.mode === "wasm") {
      stepEl.textContent = "failed: " + err.message;
      boot.classList.add("failed");
      return;
    }
    stale = true;
  }
  boot.hidden = true;
  if (stale) {
    const w = document.querySelector("#warnings");
    w.hidden = false;
    w.textContent = api.mode === "wasm"
      ? "This page mixes files from two different app versions (an old copy is "
        + "cached). Reload with Ctrl+F5 - if it persists, empty the browser cache."
      : "The server is running an OLDER version of the app than the files on disk. "
        + "Close the server console window (start.bat) and start it again, then reload this page (Ctrl+F5).";
    if (api.mode !== "wasm") {
      alert("Server restart needed:\n\nThe running server is an older version. " +
        "Close the black server console window, double-click start.bat again, then reload this page.");
    }
  }
})();

const PASS = {
  score: "#00c800",   // DLF-02 next-contour glue reference (DXF: 0,255,0; darkened for screen)
  labels: "#0000ff",  // DLF-01 contour numbers
  cutInner: "#ff00ff",// DLF-04
  cutOuter: "#ff0000",// DLF-05
};

const state = {
  uploadId: null,
  result: null,
  tab: "sheets",
  timer: null,
};

const $ = (sel) => document.querySelector(sel);
const canvas = $("#canvas");
const ctx = canvas.getContext("2d");

/* ---------- upload ---------- */
const dropzone = $("#dropzone");
const fileInput = $("#file-input");
if (!api.supportsMesh) {
  // do not invite a file the build is only going to refuse
  fileInput.accept = ".tif,.tiff";
  $("#drop-hint strong").textContent = "Drop a GeoTIFF DTM here";
}
dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener("change", () => {
  if (fileInput.files.length) uploadFile(fileInput.files[0]);
});
$("#demo-link").addEventListener("click", async (e) => {
  e.preventDefault();
  e.stopPropagation();
  setBusy(true);
  try {
    const data = await api.demo();
    fitScaleToSheet(data.summary);
    applyUpload(data);
  } catch (err) {
    alert("Demo failed: " + err.message);
  } finally {
    setBusy(false);
  }
});

/* The demo terrain is 1.5 km across, so at the default 1:500 its board is
 * 3000 mm - larger than any sheet, and the first thing a first-time visitor
 * sees would be "0 sheets". Coarsen the scale just enough that the board fits,
 * and only for the demo: a real terrain is the user's own business. */
function fitScaleToSheet(summary) {
  const scaleInput = $("input[name=scale]");
  const num = (name, fallback) => {
    const v = parseFloat($(`input[name=${name}]`).value);
    return Number.isFinite(v) && v > 0 ? v : fallback;
  };
  const margin = num("sheet_margin_mm", 10);
  const usableW = num("sheet_width_mm", 1000) - 2 * margin;
  const usableH = num("sheet_height_mm", 700) - 2 * margin;
  const w = summary.width_world * 1000;   // model mm at 1:1
  const h = summary.height_world * 1000;
  if (usableW <= 0 || usableH <= 0 || !(w > 0) || !(h > 0)) return;
  // nest() may turn a board 90 degrees, so take whichever way round is easier
  const need = Math.min(Math.max(w / usableW, h / usableH),
                        Math.max(h / usableW, w / usableH));
  const current = num("scale", 0);
  if (current >= need) return;            // already fits, leave it alone
  scaleInput.value = Math.ceil(need / 50) * 50;
}

async function uploadFile(file) {
  setBusy(true, "reading the terrain");
  try {
    applyUpload(await api.upload(file));
  } catch (err) {
    alert("Upload failed: " + err.message);
  } finally {
    setBusy(false);
  }
}

function applyUpload(data) {
  state.uploadId = data.upload_id;
  const s = data.summary;
  dropzone.classList.add("loaded");
  $("#drop-hint").hidden = true;
  $("#dtm-card").hidden = false;
  resetSheetView();
  state.hatches = []; // hatch layers are tied to the previous terrain upload
  renderHatchList();
  $("#hillshade").src = data.hillshade;
  $("#dtm-filename").textContent = data.name || "terrain";
  // one decimal everywhere, relief computed from the rounded bounds so the
  // displayed numbers always add up
  const zminR = Math.round(s.zmin * 10) / 10;
  const zmaxR = Math.round(s.zmax * 10) / 10;
  const relief = Math.round((zmaxR - zminR) * 10) / 10;
  $("#dtm-meta").innerHTML =
    `<dt>Raster</dt><dd>${s.cols} × ${s.rows} cells</dd>` +
    `<dt>Cell size</dt><dd>${fmt(s.cell_size)} m</dd>` +
    `<dt>Elevation</dt><dd>${zminR} – ${zmaxR} m</dd>` +
    `<dt>Relief (Δz)</dt><dd>${relief} m</dd>` +
    `<dt>Extent</dt><dd>${fmt(s.width_world)} × ${fmt(s.height_world)} m</dd>`;
  const notes = $("#dtm-notes");
  const warn = [...data.warnings];
  // Tell the user BEFORE the long wait, not after. At the shipped defaults
  // (1:500, 2 mm) the contour interval is 1 m, so a 400 m mountain asks for
  // 400 contours - minutes of work in the browser. Measured: ~300 contours is
  // roughly 14 s here, and several times that on a modest laptop.
  const dz = s.zmax - s.zmin;                 // NB: `relief` is already taken above
  const interval = dz > 0 ? estimatedInterval() : 0;
  const levels = interval > 0 ? Math.floor(dz / interval) : 0;
  if (levels > 150) {
    warn.push(
      `this terrain has ${Math.round(dz)} m of relief, which at the current ` +
      `scale means about ${levels} contours - slicing will take a while. ` +
      `A coarser scale or thicker material gives fewer, chunkier layers.`);
  }
  notes.hidden = warn.length === 0;
  notes.textContent = warn.join(" · ");
  requestSlice(true);
}

/** Contour interval in metres implied by the current form values, or 0 if it
 *  cannot be determined. Returns rather than throws: this only drives an
 *  advisory note, and it is called from applyUpload() - an exception here
 *  would abort the upload before requestSlice() and leave the app looking
 *  dead, which is precisely the failure it exists to warn about. */
function estimatedInterval() {
  try {
    const num = (name) => {
      const el = $(`input[name=${name}]`);
      return el ? parseFloat(el.value) : NaN;
    };
    const th = num("thickness_mm");
    const sc = num("scale");
    const ex = num("vertical_exaggeration") || 1;
    if (!(th > 0) || !(sc > 0) || !(ex > 0)) return 0;
    return (th * sc) / 1000 / ex;
  } catch {
    return 0;
  }
}

/* ---------- hatch shapefiles (multiple layers, each with own settings) ---------- */
const HATCH_PREVIEW = { blue: "#0000ff", green: "#00c800", cyan: "#00b4b4", magenta: "#ff00ff" };
const HATCH_PATTERN_GROUPS = [
  ["Linear", [["lines", "Lines"], ["double", "Double lines"], ["dashes", "Dashes"],
    ["dashdot", "Dash-dot"], ["cross", "Crosshatch"], ["trigrid", "Triangle grid"],
    ["zigzag", "Zigzag"]]],
  ["Water", [["waves", "Waves"], ["ripples", "Ripples"], ["scales", "Fish scales"]]],
  ["Paving", [["herringbone", "Herringbone"], ["brick", "Running bond"],
    ["hex", "Honeycomb"], ["diamonds", "Diamonds"]]],
  ["Scatter & vegetation", [["dots", "Dots"], ["rings", "Rings"], ["stipple", "Stipple"],
    ["pebbles", "Pebbles"], ["plus", "Plus marks"], ["ticks", "Ticks"],
    ["grass", "Grass tufts"], ["marsh", "Marsh reeds"]]],
  ["Abstract", [["interference", "Interference"], ["echo", "Contour echo"]]],
];
const LINETYPES = [["solid", "Solid"], ["dashed", "Dashed"], ["dotted", "Dotted"],
  ["dashdot", "Dash-dot"], ["dashdotdot", "Dash-dot-dot"]];
state.hatches = []; // {id, kind, name, ...per-kind settings}

// the browser must never open a dropped file as a page
for (const ev of ["dragover", "drop"]) {
  window.addEventListener(ev, (e) => e.preventDefault());
}

for (const zone of document.querySelectorAll(".shp-drop")) {
  const input = zone.querySelector("input[type=file]");
  zone.addEventListener("click", () => input.click());
  input.addEventListener("change", async () => {
    for (const file of input.files) await uploadHatch(file, zone.dataset.kind);
    input.value = "";
  });
  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("dragover");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
  zone.addEventListener("drop", async (e) => {
    e.preventDefault();
    zone.classList.remove("dragover");
    for (const file of e.dataTransfer.files) await uploadHatch(file, zone.dataset.kind);
  });
}

function defaultLayerSettings(kind, index) {
  const color = ["green", "blue", "cyan"][index % 3];
  if (kind === "line") return { linetype: "dashed", linetype_scale: 1, color };
  if (kind === "point") return { radius_mm: 2, linetype: "solid", linetype_scale: 1,
    point_hatch: "none", point_hatch_spacing_mm: 1, color };
  return { pattern: "lines", spacing_mm: 2, rotation_deg: 45, outline: true, color };
}

async function uploadHatch(file, expectedKind) {
  if (!state.uploadId) {
    alert("Load a terrain file first, then add the shapefile.");
    return;
  }
  setBusy(true);
  try {
    const data = await api.hatchAdd(state.uploadId, file);
    const sameKind = state.hatches.filter((h) => h.kind === data.kind).length;
    state.hatches.push({
      id: data.hatch_id, kind: data.kind, name: data.name,
      ...defaultLayerSettings(data.kind, sameKind),
    });
    renderHatchList();
    const notes = [...data.warnings];
    if (expectedKind && data.kind !== expectedKind) {
      notes.push(`'${data.name}' contains ${data.kind} features - added to the ${data.kind} section`);
    }
    if (notes.length) {
      $("#warnings").hidden = false;
      $("#warnings").textContent = notes.join("\n");
    }
    requestSlice(true);
  } catch (err) {
    alert("Shapefile failed: " + err.message);
  } finally {
    setBusy(false);
  }
}

async function removeHatch(id) {
  if (state.uploadId) {
    await api.hatchRemove(state.uploadId, id);
  }
  state.hatches = state.hatches.filter((h) => h.id !== id);
  renderHatchList();
  requestSlice(true);
}

function patternSelectHTML(selected) {
  return HATCH_PATTERN_GROUPS.map(([group, items]) =>
    `<optgroup label="${group}">` + items.map(([key, label]) =>
      `<option value="${key}"${key === selected ? " selected" : ""}>${label}</option>`
    ).join("") + "</optgroup>").join("");
}

function linetypeSelectHTML(selected) {
  return LINETYPES.map(([key, label]) =>
    `<option value="${key}"${key === selected ? " selected" : ""}>${label}</option>`).join("");
}

function colorSelectHTML(selected, withMagenta = false) {
  const names = { blue: "light", green: "medium", cyan: "strong", magenta: "CUT" };
  const colors = withMagenta ? ["blue", "green", "cyan", "magenta"] : ["blue", "green", "cyan"];
  return colors.map((c) =>
    `<option value="${c}"${c === selected ? " selected" : ""}>` +
    `${c[0].toUpperCase() + c.slice(1)} &middot; ${names[c]}</option>`
  ).join("");
}

function cardBodyHTML(h) {
  if (h.kind === "line") {
    return `
      <label><span>Linetype</span>
        <select data-key="linetype">${linetypeSelectHTML(h.linetype)}</select></label>
      <label><span>Scale <em>dash length</em></span>
        <input type="number" data-key="linetype_scale" value="${h.linetype_scale}" min="0.25" step="0.25"></label>
      <label><span>Color / intensity</span>
        <select data-key="color">${colorSelectHTML(h.color)}</select></label>`;
  }
  if (h.kind === "point") {
    return `
      <label><span>Circle radius <em>mm</em></span>
        <input type="number" data-key="radius_mm" value="${h.radius_mm}" min="0.2" step="0.5"></label>
      <label><span>Linetype</span>
        <select data-key="linetype">${linetypeSelectHTML(h.linetype)}</select></label>
      <label><span>Scale <em>dash length</em></span>
        <input type="number" data-key="linetype_scale" value="${h.linetype_scale}" min="0.25" step="0.25"></label>
      <label><span>Hatch <em>inside circle</em></span>
        <select data-key="point_hatch"><option value="none"${h.point_hatch === "none" ? " selected" : ""}>None</option>${patternSelectHTML(h.point_hatch)}</select></label>
      <label><span>Hatch spacing <em>mm</em></span>
        <input type="number" data-key="point_hatch_spacing_mm" value="${h.point_hatch_spacing_mm}" min="0.2" step="0.25"></label>
      <label><span>Color / intensity</span>
        <select data-key="color">${colorSelectHTML(h.color, true)}</select></label>`;
  }
  return `
    <label><span>Pattern</span>
      <select data-key="pattern">${patternSelectHTML(h.pattern)}</select></label>
    <label><span>Spacing <em>mm</em></span>
      <input type="number" data-key="spacing_mm" value="${h.spacing_mm}" min="0.2" step="0.5"></label>
    <label><span>Rotation <em>&deg;</em></span>
      <input type="number" data-key="rotation_deg" value="${h.rotation_deg}" min="-180" max="180" step="15"></label>
    <label class="check"><input type="checkbox" data-key="outline"${h.outline ? " checked" : ""}>
      <span>Score outline</span></label>
    <label><span>Color / intensity</span>
      <select data-key="color">${colorSelectHTML(h.color)}</select></label>`;
}

function renderHatchList() {
  const lists = {
    polygon: $("#hatch-list"), line: $("#line-list"), point: $("#point-list"),
  };
  for (const list of Object.values(lists)) list.innerHTML = "";
  for (const h of state.hatches) {
    const list = lists[h.kind] || lists.polygon;
    list.insertAdjacentHTML("beforeend", `
      <div class="hatch-card" data-id="${h.id}">
        <div class="hatch-head">
          <i class="dot" style="background:${HATCH_PREVIEW[h.color]}"></i>
          <b title="${h.name}">${h.name}</b>
          <a href="#" data-remove>remove</a>
        </div>
        ${cardBodyHTML(h)}
      </div>`);
  }
  for (const card of document.querySelectorAll(".hatch-card")) {
    const entry = state.hatches.find((h) => h.id === card.dataset.id);
    card.querySelector("[data-remove]").addEventListener("click", (e) => {
      e.preventDefault();
      removeHatch(card.dataset.id);
    });
    for (const el of card.querySelectorAll("[data-key]")) {
      el.addEventListener("input", () => {
        entry[el.dataset.key] = el.type === "checkbox" ? el.checked
          : el.tagName === "SELECT" ? el.value : parseFloat(el.value);
        if (el.dataset.key === "color") {
          card.querySelector(".dot").style.background = HATCH_PREVIEW[entry.color];
        }
      });
    }
  }
}

/* ---------- parameters ---------- */
function readParams() {
  const p = {};
  for (const el of $("#params").elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") p[el.name] = el.checked;
    else if (el.tagName === "SELECT") p[el.name] = el.value;
    else p[el.name] = parseFloat(el.value);
  }
  p.hatch_layers = state.hatches.map(({ name, ...settings }) => settings);
  return p;
}
$("#params").addEventListener("input", () => requestSlice());

/* The 400 ms wait is there for the parameter form, which fires on every
 * keystroke and every slider tick. Discrete actions - a terrain or a
 * shapefile arriving, a layer removed - have nothing to coalesce, so they
 * ask for the slice straight away instead of staring at an unchanged
 * viewport first. */
function requestSlice(immediate = false) {
  if (!state.uploadId) return;
  clearTimeout(state.timer);
  state.timer = setTimeout(doSlice, immediate ? 0 : 400);
}

async function doSlice() {
  setBusy(true, "slicing");
  try {
    state.result = await api.slice(state.uploadId, readParams());
    $("#empty-state").hidden = true;
    $("#export").disabled = state.result.stats.n_sheets === 0;
    renderStats();
    stackDirty = true;
    draw();
  } catch (err) {
    // Clear the previous run's numbers. They belonged to the last slice that
    // WORKED, and leaving them under an error message reads as if the failed
    // settings had produced them - the stats said "13 contours - 4 boards -
    // 4 sheets" while the model had in fact not been built at all.
    state.result = null;
    $("#stats").hidden = true;
    $("#export").disabled = true;
    stackDirty = true;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    draw();  // hides the stack tools and gizmo; guards on a null result
    $("#warnings").hidden = false;
    $("#warnings").textContent = err.message;
  } finally {
    setBusy(false);
  }
}

function renderStats() {
  const st = state.result.stats;
  $("#stats").hidden = false;
  $("#stats").innerHTML =
    `contour interval <b>${fmt(st.world_interval)} m</b> real world<br>` +
    `<b>${st.n_levels}</b> contours · <b>${st.n_boards}</b> boards · <b>${st.n_sheets}</b> sheets<br>` +
    `model ${fmt(st.model_width)} × ${fmt(st.model_height)} mm` +
    (st.glue_min != null ? `<br>narrowest glue strip <b>${fmt(st.glue_min)} mm</b>` : "");
  const warn = state.result.warnings;
  $("#warnings").hidden = warn.length === 0;
  $("#warnings").textContent = warn.join("\n");
}

/* ---------- export ---------- */
$("#export").addEventListener("click", async () => {
  setBusy(true);
  try {
    const { blob, filename } = await api.export(state.uploadId, readParams());
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (err) {
    alert("Export failed: " + err.message);
  } finally {
    setBusy(false);
  }
});

/* ---------- tabs ---------- */
for (const btn of document.querySelectorAll(".tab")) {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.tab = btn.dataset.tab;
    draw();
  });
}
window.addEventListener("resize", draw);
new ResizeObserver(() => draw()).observe($("#stack3d"));
new ResizeObserver(() => { if (state.tab === "sheets") draw(); }).observe(canvas);

function draw() {
  const isStack = state.tab === "stack";
  canvas.hidden = isStack;
  $("#stack3d").hidden = !isStack;
  $("#stack-tools").hidden = !state.result;
  $("#gizmo").hidden = !state.result;
  $("#stack-hint").hidden = !isStack || !state.result;
  $("#sheets-hint").hidden = isStack || !state.result;
  canvas.classList.toggle("pannable", !isStack && !!state.result);
  if (!state.result) return;
  if (isStack) {
    resizeThree();
    if (stackDirty) buildStack3D();
  } else {
    drawSheets();
  }
}

/* ---------- sheet view zoom & pan ---------- */
const sheetView = { z: 1, x: 0, y: 0, drag: null };
function resetSheetView() {
  sheetView.z = 1; sheetView.x = 0; sheetView.y = 0;
}
canvas.addEventListener("wheel", (e) => {
  if (state.tab !== "sheets" || !state.result) return;
  e.preventDefault();
  const r = canvas.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  const factor = Math.exp(-e.deltaY * 0.0015);
  const nz = Math.min(50, Math.max(0.2, sheetView.z * factor));
  const k = nz / sheetView.z;
  sheetView.x = mx - (mx - sheetView.x) * k;
  sheetView.y = my - (my - sheetView.y) * k;
  sheetView.z = nz;
  drawSheets();
}, { passive: false });
canvas.addEventListener("mousedown", (e) => {
  if (state.tab !== "sheets" || !state.result) return;
  sheetView.drag = { mx: e.clientX, my: e.clientY, x: sheetView.x, y: sheetView.y };
  canvas.classList.add("dragging");
});
window.addEventListener("mousemove", (e) => {
  if (!sheetView.drag) return;
  sheetView.x = sheetView.drag.x + (e.clientX - sheetView.drag.mx);
  sheetView.y = sheetView.drag.y + (e.clientY - sheetView.drag.my);
  drawSheets();
});
window.addEventListener("mouseup", () => {
  sheetView.drag = null;
  canvas.classList.remove("dragging");
});
canvas.addEventListener("dblclick", () => {
  resetSheetView();
  if (state.tab === "sheets" && state.result) drawSheets();
});

/* ---------- 2D sheet preview ---------- */
function fitCanvas() {
  const r = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = r.width * dpr;
  canvas.height = r.height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { w: r.width, h: r.height };
}

function polyPath(pts, sx, sy) {
  ctx.beginPath();
  pts.forEach(([x, y], i) => {
    i ? ctx.lineTo(sx(x), sy(y)) : ctx.moveTo(sx(x), sy(y));
  });
}

function drawSheets() {
  const { w, h } = fitCanvas();
  ctx.clearRect(0, 0, w, h);
  ctx.translate(sheetView.x, sheetView.y);
  ctx.scale(sheetView.z, sheetView.z);
  const z = sheetView.z;
  const { sheet, placements, stats } = state.result;

  // Nothing nested: say so HERE, in the viewport the user is looking at. This
  // used to fall through to Math.max(1, n_sheets) and draw one phantom empty
  // sheet labelled "sheet 1 - board" with nothing on it, while the real reason
  // ("board larger than the usable sheet ...") went to #warnings in the sidebar,
  // below the parameters and off-screen. A user reported that as the app doing
  // nothing at all - the slice had in fact finished in under a second.
  if (!stats.n_sheets) {
    ctx.setTransform(window.devicePixelRatio || 1, 0, 0, window.devicePixelRatio || 1, 0, 0);
    const usableW = sheet.w - 2 * sheet.margin, usableH = sheet.h - 2 * sheet.margin;
    const lines = [
      "Nothing fits on a sheet yet.",
      `The model is ${Math.round(stats.model_width)} × ${Math.round(stats.model_height)} mm;`
        + ` a sheet holds ${Math.round(usableW)} × ${Math.round(usableH)} mm inside the margin.`,
      "Use a larger scale number, or a bigger material sheet.",
    ];
    ctx.textAlign = "center";
    ctx.fillStyle = "#111";
    ctx.font = '600 15px "Source Sans 3", system-ui';
    ctx.fillText(lines[0], w / 2, h / 2 - 14);
    ctx.fillStyle = "#6d6f72";
    ctx.font = '13px "Quattrocento Sans", system-ui';
    ctx.fillText(lines[1], w / 2, h / 2 + 8);
    ctx.fillText(lines[2], w / 2, h / 2 + 28);
    ctx.textAlign = "start";
    return;
  }

  const n = stats.n_sheets;
  const pad = 20;
  let cols = Math.ceil(Math.sqrt((n * sheet.h * (w - pad)) / (sheet.w * (h - pad))));
  cols = Math.min(n, Math.max(1, cols));
  const rows = Math.ceil(n / cols);
  const scale = Math.min(
    (w - pad * (cols + 1)) / (cols * sheet.w),
    (h - pad * (rows + 1)) / (rows * sheet.h));

  for (let s = 0; s < n; s++) {
    const gx = s % cols, gy = Math.floor(s / cols);
    const ox = pad + gx * (sheet.w * scale + pad);
    const oy = pad + gy * (sheet.h * scale + pad);
    const sx = (x) => ox + x * scale;
    const sy = (y) => oy + (sheet.h - y) * scale; // y up like the DXF

    // sheet as linework only: no fill, dark gray border + dashed margin
    ctx.strokeStyle = "#4a4a4a";
    ctx.lineWidth = 1 / z;
    ctx.strokeRect(ox, oy, sheet.w * scale, sheet.h * scale);
    ctx.setLineDash([4 / z, 4 / z]);
    ctx.strokeStyle = "#8a8a8a";
    ctx.strokeRect(ox + sheet.margin * scale, oy + sheet.margin * scale,
      (sheet.w - 2 * sheet.margin) * scale, (sheet.h - 2 * sheet.margin) * scale);
    ctx.setLineDash([]);
    ctx.fillStyle = "#6a6a6a";
    ctx.font = `${11 / z}px "Source Sans 3", system-ui`;
    const boards = placements.filter((p) => p.sheet === s).map((p) => p.board);
    ctx.fillText(`sheet ${s + 1} · board ${boards.join(", ")}`, ox, oy - 5 / z);

    for (const p of placements) {
      if (p.sheet !== s) continue;
      ctx.lineWidth = 1.2 / z;
      ctx.strokeStyle = PASS.cutOuter;
      polyPath(p.outer, sx, sy); ctx.closePath(); ctx.stroke();
      ctx.strokeStyle = PASS.cutInner;
      for (const line of p.holes) { polyPath(line, sx, sy); ctx.stroke(); }
      ctx.lineWidth = 1 / z;
      ctx.strokeStyle = PASS.score;
      for (const line of p.score) { polyPath(line, sx, sy); ctx.stroke(); }
      ctx.strokeStyle = PASS.labels;
      for (const line of p.labels) { polyPath(line, sx, sy); ctx.stroke(); }
      if (p.hatch && p.hatch.length) {
        ctx.lineWidth = 0.8 / z;
        ctx.lineCap = "round"; // dots of dotted linetypes stay visible at any zoom
        for (const entry of p.hatch) {
          if (!entry || !Array.isArray(entry.lines)) continue; // old-server payload: skip, never crash
          ctx.strokeStyle = HATCH_PREVIEW[entry.color] || "#00c800";
          for (const line of entry.lines) { polyPath(line, sx, sy); ctx.stroke(); }
        }
        ctx.lineCap = "butt";
      }
    }
  }
}

/* ---------- 3D stack view (Three.js, vendored) ---------- */
let three = null;
let stackDirty = true;

function ensureThree() {
  if (three) return three;
  const container = $("#stack3d");
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio || 1);
  container.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xffffff);
  // Fades the ground grid out into the background so it reads as infinite
  // instead of ending at a visible square edge. Fog is applied per fragment,
  // so the fade is smooth ALONG each grid line - a vertex-colour fade cannot
  // do this, because every grid line spans the whole extent and both of its
  // endpoints sit at maximum radius. Range is refreshed per frame in the loop
  // below so the effect holds at any zoom. Only the grid is fogged; the
  // terrain materials opt out with `fog: false`.
  scene.fog = new THREE.Fog(0xffffff, 1, 2);
  const camera = new THREE.PerspectiveCamera(42, 1, 0.5, 500000);
  camera.up.set(0, 0, 1);
  const controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.12;
  /* The orthographic camera mirrors the perspective one every frame - same
   * position, same look-at, frustum sized from the perspective FOV at the
   * current orbit distance, so toggling projections reads as a change of
   * projection rather than a jump. OrbitControls keeps driving the
   * perspective camera either way; ortho is only ever a projection mirror. */
  const orthoCamera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.5, 500000);
  orthoCamera.up.set(0, 0, 1);
  scene.add(new THREE.AmbientLight(0xffffff, 0.55));
  const sun = new THREE.DirectionalLight(0xffffff, 0.75);
  sun.position.set(0.6, -1, 1.4);
  scene.add(sun);
  const fill = new THREE.DirectionalLight(0xffffff, 0.25);
  fill.position.set(-1, 0.5, 0.6);
  scene.add(fill);
  const group = new THREE.Group();
  scene.add(group);
  // click (not drag) picks a ring: highlight it and show its number
  const ray = new THREE.Raycaster();
  let downAt = null;
  renderer.domElement.addEventListener("pointerdown", (e) => {
    downAt = [e.clientX, e.clientY];
  });
  renderer.domElement.addEventListener("pointerup", (e) => {
    const wasClick = downAt &&
      Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) < 6;
    downAt = null;
    if (!wasClick || !stackView.items.length) return;
    const r = renderer.domElement.getBoundingClientRect();
    const mouse = new THREE.Vector2(
      ((e.clientX - r.left) / r.width) * 2 - 1,
      -((e.clientY - r.top) / r.height) * 2 + 1);
    const pickCam = axis.orthographic ? orthoCamera : camera;
    pickCam.updateMatrixWorld(true);
    ray.setFromCamera(mouse, pickCam);
    const hits = ray.intersectObjects(
      stackView.items.filter((it) => it.mesh.visible).map((it) => it.mesh));
    const lvl = hits.length ? hits[0].object.userData.level : null;
    stackView.selectedLevel = lvl === stackView.selectedLevel ? null : lvl;
    applyStackAppearance();
  });
  /* ---- axis views (Top/Front/..., ported from DL-TerrainCare) ----
   * Orthographic by default, because a "top view" that still converges is not
   * a plan. Pressing the same button again RETURNS to the view you were in
   * before; the stored return view survives switching between axis views and
   * is discarded the moment the camera is moved by hand. */
  const axis = { orthographic: false, ret: null, anim: null };
  const nearlyTop = 1.5533; // just under pi/2: exactly vertical loses azimuth
  const AXIS_VIEWS = {
    top: { yaw: 0, pitch: nearlyTop },
    bottom: { yaw: 0, pitch: -nearlyTop },
    front: { yaw: Math.PI, pitch: 0.02 },   // looking north, from the south
    back: { yaw: 0, pitch: 0.02 },          // looking south, from the north
    right: { yaw: Math.PI / 2, pitch: 0.02 },
    left: { yaw: -Math.PI / 2, pitch: 0.02 },
  };
  const camState = () => ({
    pos: camera.position.clone(), target: controls.target.clone(),
  });
  function moveCamera(to, seconds) {
    if (seconds <= 0) {
      camera.position.copy(to.pos);
      controls.target.copy(to.target);
      controls.update();
      return;
    }
    axis.anim = { from: camState(), to, t0: performance.now(), dur: seconds * 1000 };
  }
  function setAxisView(name) {
    const v = AXIS_VIEWS[name];
    if (!v) return false;
    if (axis.ret && axis.ret.name === name) {           // second press: go back
      const back = axis.ret;
      axis.ret = null;
      axis.orthographic = back.orthographic;            // projection is part of the view
      syncProjButton();
      moveCamera(back.state, 0.45);
      return true;
    }
    if (axis.ret) axis.ret.name = name;                 // axis-to-axis: keep the origin
    else axis.ret = { name, state: camState(), orthographic: axis.orthographic };
    axis.orthographic = true;
    syncProjButton();
    const t = controls.target;
    const dist = camera.position.distanceTo(t);
    const cp = Math.cos(v.pitch), sp = Math.sin(v.pitch);
    moveCamera({
      pos: new THREE.Vector3(t.x + dist * cp * Math.sin(v.yaw),
                             t.y + dist * cp * Math.cos(v.yaw),
                             t.z + dist * sp),
      target: t.clone(),
    }, 0.45);
    return false;
  }
  function syncProjButton() {
    const b = $("#proj");
    b.textContent = axis.orthographic ? "Orthographic" : "Perspective";
    b.classList.toggle("on", axis.orthographic);
  }
  for (const b of document.querySelectorAll("#gizmo .grid button")) {
    b.addEventListener("click", () => setAxisView(b.dataset.view));
  }
  $("#proj").addEventListener("click", () => {
    axis.orthographic = !axis.orthographic;
    syncProjButton();
  });
  // a hand-moved camera means the axis view stopped being "a detour from" anywhere
  controls.addEventListener("start", () => { axis.ret = null; axis.anim = null; });

  three = { renderer, scene, camera, controls, group };
  (function loop() {
    requestAnimationFrame(loop);
    if (state.tab === "stack" && !$("#stack3d").hidden) {
      if (axis.anim) {
        const a = axis.anim;
        const u = Math.min(1, (performance.now() - a.t0) / a.dur);
        const e = 0.5 - 0.5 * Math.cos(Math.PI * u);    // inOutSine
        camera.position.lerpVectors(a.from.pos, a.to.pos, e);
        controls.target.lerpVectors(a.from.target, a.to.target, e);
        if (u >= 1) axis.anim = null;
      }
      controls.update();
      // keep the grid's horizon proportional to how far the camera is out,
      // so it fades at the same visual depth whether zoomed in or out
      const d = camera.position.distanceTo(controls.target);
      scene.fog.near = d * 1.0;
      scene.fog.far = d * 3.0;
      // Track the clip planes to the orbit distance instead of fixing them once
      // from the model size. The old dim/500 .. dim*40 pair spanned 20000:1, and
      // a depth buffer stretched that far cannot separate the ground grid from
      // the base plate sitting on top of it. Following d keeps the ratio at
      // 2000:1 at every zoom, so near is always 1% of the viewing distance -
      // close enough never to clip what you zoomed in to look at, far enough
      // that the grid stays behind the model. fog.far (3d) hides the grid long
      // before far (20d) could clip it.
      const near = d / 100, far = d * 20;
      if (camera.near !== near || camera.far !== far) {
        camera.near = near; camera.far = far;
        camera.updateProjectionMatrix();
      }
      let cam = camera;
      if (axis.orthographic) {
        const o = orthoCamera;
        o.position.copy(camera.position);
        o.quaternion.copy(camera.quaternion);
        const halfH = Math.tan((camera.fov * Math.PI) / 360) * d;
        const halfW = halfH * (camera.aspect || 1);
        o.left = -halfW; o.right = halfW; o.top = halfH; o.bottom = -halfH;
        o.near = camera.near; o.far = camera.far;
        o.updateProjectionMatrix();
        cam = o;
      }
      renderer.render(scene, cam);
    }
  })();
  return three;
}

function resizeThree() {
  const t = ensureThree();
  const el = $("#stack3d");
  const w = el.clientWidth || 400, h = el.clientHeight || 300;
  t.renderer.setSize(w, h);
  t.camera.aspect = w / h;
  t.camera.updateProjectionMatrix();
}

function clearGroup(group) {
  while (group.children.length) {
    const c = group.children.pop();
    if (c.geometry) c.geometry.dispose();
    if (c.material) {
      if (c.material.map) c.material.map.dispose();
      c.material.dispose();
    }
  }
}

// assembly-aid state; boards/explode survive re-slices, selection/step reset
const stackView = {
  boards: false, explode: 0, step: null, selectedLevel: null,
  items: [], levels: [], th: 2, nBoards: 2, nLevels: 1, zLevels: [],
};

function buildStack3D() {
  const t = ensureThree();
  clearGroup(t.group);
  const { stack, stats, z_levels } = state.result;
  const th = readParams().thickness_mm || 2;
  const n = stats.n_levels;
  const W = stats.model_width, H = stats.model_height;
  Object.assign(stackView, {
    items: [], th, nBoards: stats.n_boards,
    nLevels: n, zLevels: z_levels || [], step: null, selectedLevel: null,
  });

  for (const e of stack) {
    const shape = new THREE.Shape(e.outer.map(([x, y]) => new THREE.Vector2(x, y)));
    for (const hole of e.holes) {
      shape.holes.push(new THREE.Path(hole.map(([x, y]) => new THREE.Vector2(x, y))));
    }
    const geo = new THREE.ExtrudeGeometry(shape, { depth: th, bevelEnabled: false, curveSegments: 4 });
    // fog:false - the scene fog exists only to fade the ground grid out;
    // the model itself must stay crisp at any distance
    const mesh = new THREE.Mesh(geo, new THREE.MeshLambertMaterial({ fog: false }));
    mesh.userData.level = e.level;
    t.group.add(mesh);
    const edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(geo, 25),
      new THREE.LineBasicMaterial({ color: 0x3c3c3c, transparent: true, opacity: 0.4, fog: false }));
    t.group.add(edges);
    stackView.items.push({ mesh, edges, level: e.level, board: e.level % stats.n_boards });
  }
  stackView.levels = [...new Set(stackView.items.map((it) => it.level))].sort((a, b) => a - b);

  // ground grid (50 mm cells, or coarser for big models). The grid is drawn far
  // past the model and fogged out (see scene.fog) so no edge is ever visible -
  // cell size still follows the MODEL, not the extended grid.
  const span = Math.max(W, H);
  const cell = span * 1.7 > 1500 ? 100 : 50;
  const size = span * 14;
  // Both colours are the SAME on purpose. GridHelper's first colour argument is
  // the CENTRE-LINE colour, and a darker one draws a cross through the middle of
  // the grid - which, because the grid is centred on the model, runs underneath
  // the model and out the far side. Combined with the fog range below (which
  // leaves anything nearer than the camera target unfaded) that read as a solid
  // dark bar lying on the ground in front of the model. Reported as a slicing
  // artefact; it never was one.
  const grid = new THREE.GridHelper(size, Math.max(2, Math.round(size / cell)), 0xe6e6e6, 0xe6e6e6);
  grid.rotation.x = Math.PI / 2;
  // Sit the grid a fraction of the MODEL below z=0, not a fixed 0.1 mm. The base
  // plate's underside is at z=0, and 0.1 mm is far too tight for the depth buffer
  // to resolve - the grid punched up through the model, worst on nearly flat
  // terrain (a difference raster is ~90% flat, so almost the whole model sits
  // exactly where the grid is). Proportional keeps it invisibly close at any size.
  grid.position.set(W / 2, H / 2, -Math.max(0.5, span / 200));
  t.group.add(grid);

  const topZ = Math.max(1, n * th);

  applyStackAppearance();
  applyExplode();
  updateStepUI();

  // frame the model
  const dim = Math.max(W, H, topZ);
  t.controls.target.set(W / 2, H / 2, topZ / 2);
  t.camera.position.set(W / 2 - dim * 0.55, H / 2 - dim * 1.05, topZ / 2 + dim * 0.75);
  // Seed values only - the render loop retunes these to the orbit distance every
  // frame (see the clip-plane note there). Kept so the very first frame, drawn
  // before the loop has run once, is already sane.
  t.camera.near = dim / 100;
  t.camera.far = dim * 20;
  t.camera.updateProjectionMatrix();
  t.controls.update();
  stackDirty = false;
}

function currentStepLevel() {
  return stackView.step === null ? null : stackView.levels[stackView.step];
}

function applyStackAppearance() {
  const cur = currentStepLevel();
  for (const it of stackView.items) {
    const visible = cur === null || it.level <= cur;
    it.mesh.visible = visible;
    it.edges.visible = visible;
    // base shade: elevation ramp, or one shade per physical board
    let g;
    if (stackView.boards) {
      const N = stackView.nBoards;
      g = N > 1 ? 0.3 + 0.58 * (it.board / (N - 1)) : 0.6;
    } else {
      g = 0.55 + 0.34 * (it.level / Math.max(1, stackView.nLevels - 1));
    }
    const hot = it.level === cur || it.level === stackView.selectedLevel;
    if (hot) {
      it.mesh.material.color.setRGB(0.23, 0.21, 0.18); // ink - the ring in focus
    } else if (stackView.selectedLevel !== null) {
      const d = g * 0.35 + 0.62; // fade the rest so the picked ring stands out
      it.mesh.material.color.setRGB(d, d, d);
    } else {
      it.mesh.material.color.setRGB(g, g, g);
    }
  }
  updateStackInfo();
}

function applyExplode() {
  const f = 1 + 3 * stackView.explode;
  const th = stackView.th;
  for (const it of stackView.items) {
    it.mesh.position.z = it.level * th * f;
    it.edges.position.z = it.level * th * f;
  }
}

function updateStackInfo() {
  const info = $("#stack-info");
  const lvl = stackView.selectedLevel ?? currentStepLevel();
  if (lvl == null || !state.result) {
    info.hidden = true;
    return;
  }
  const z = stackView.zLevels[lvl];
  info.hidden = false;
  info.innerHTML = `contour <b>${lvl}</b> &middot; board <b>${lvl % stackView.nBoards}</b>` +
    (z !== undefined ? ` &middot; <b>${fmt(z)} m</b>` : "");
}

function updateStepUI() {
  $("#st-step").textContent = stackView.step === null ? "all"
    : `${stackView.step + 1} / ${stackView.levels.length}`;
}

function stepBy(d) {
  if (!stackView.items.length) return;
  const len = stackView.levels.length;
  let s = stackView.step;
  if (d > 0) s = s === null ? 0 : (s + 1 >= len ? null : s + 1);
  else s = s === null ? len - 1 : (s - 1 < 0 ? null : s - 1);
  stackView.step = s;
  stackView.selectedLevel = null;
  applyStackAppearance();
  updateStepUI();
}

$("#st-boards").addEventListener("click", (e) => {
  stackView.boards = !stackView.boards;
  e.currentTarget.classList.toggle("active", stackView.boards);
  if (stackView.items.length) applyStackAppearance();
});
$("#st-explode").addEventListener("input", (e) => {
  stackView.explode = parseFloat(e.target.value);
  if (stackView.items.length) applyExplode();
});
$("#st-prev").addEventListener("click", () => stepBy(-1));
$("#st-next").addEventListener("click", () => stepBy(1));

/* ---------- helpers ---------- */
function getCss(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#ccc";
}
function fmt(v) {
  return Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 1 ? +v.toFixed(1) + "" : v.toPrecision(2);
}
/* Counted, not a plain flag: reading a shapefile and the re-slice it
 * triggers overlap, and whichever finished first used to switch the
 * indicator off while the other was still working. */
let busyCount = 0;
/* The busy indicator counts overlapping operations, and now also TICKS: a real
 * DEM at the default 1:500 can ask for several hundred contours, which is tens
 * of seconds of honest work in the browser. Previously the only sign of life
 * was the static word "slicing…", so a slow slice was indistinguishable from a
 * hung app - which is exactly what a first user reported. */
let busyTimer = null;
let busyStart = 0;
function setBusy(b, label) {
  busyCount = Math.max(0, busyCount + (b ? 1 : -1));
  const on = busyCount > 0;
  $("#busy").hidden = !on;
  if (on && busyTimer === null) {
    busyStart = Date.now();
    const base = label || "working";
    const tick = () => {
      const s = Math.round((Date.now() - busyStart) / 1000);
      $("#busy-text").textContent = s < 3 ? base + "…"
        : s < 20 ? `${base}… ${s}s`
        : `${base}… ${s}s · large model, still going`;
    };
    tick();
    busyTimer = setInterval(tick, 500);
  } else if (!on && busyTimer !== null) {
    clearInterval(busyTimer);
    busyTimer = null;
  }
}
