/* The floating menu: one panel open at a time, remembered sub-folds, badges
 * on closed panels, and a window that drags by its header, folds to a chip
 * and remembers both.
 *
 * Ported from DL-TerrainDiversity terraincare/static/sidebar.js (Apache-2.0,
 * same author), trimmed to what the Slicer uses, so the DL tools look and
 * navigate alike. DOM only: app.js does not know this file exists; the badges
 * read the same controls the user sees, so they cannot disagree with them. */

const KEY = "dl-terrainslicer.sections";
const PANEL_KEY = "dl-terrainslicer.panel";
// the work order, top to bottom: load, slice, lay out, number, engrave, export
const PANELS = ["import", "slice", "nest", "label", "engrave", "export"];
// sub-folds are settings inside a panel and remember their state; the panels
// themselves open as shipped on every load (Import - a terrain comes first)
const SUBS = ["slice-detail", "eng-polygon", "eng-line", "eng-point"];
const $ = (id) => document.getElementById(id);
const menu = $("sidebar");
const chip = $("menu-chip");
const fold = $("menu-min");
const field = (name) => document.querySelector(`#params [name=${name}]`);

/* ------------------------------------------------------------- the folds */

try {
  const stored = JSON.parse(localStorage.getItem(KEY) || "null");
  for (const s of SUBS) if (stored && typeof stored[s] === "boolean" && $(`sec-${s}`)) $(`sec-${s}`).open = stored[s];
} catch { /* storage unavailable: the folds still work, they just forget */ }

function persist() {
  const out = {};
  for (const s of SUBS) if ($(`sec-${s}`)) out[s] = $(`sec-${s}`).open;
  try { localStorage.setItem(KEY, JSON.stringify(out)); } catch { /* as above */ }
}
for (const s of SUBS) $(`sec-${s}`)?.addEventListener("toggle", persist);

// opening a panel closes the others; the flag stops the cascade re-entering
let cascading = false;
for (const s of PANELS) {
  const el = $(`sec-${s}`);
  el?.addEventListener("toggle", () => {
    if (!el.open || cascading) return;
    cascading = true;
    for (const other of PANELS) if (other !== s && $(`sec-${other}`)) $(`sec-${other}`).open = false;
    cascading = false;
  });
}

/* ------------------------------------------------------------- the badges */

// what a CLOSED panel holds, beside its title (hidden while open by CSS)
function badgeText(s) {
  switch (s) {
    case "import": return $("dtm-card").hidden ? "" : $("dtm-filename").textContent;
    case "slice": return `1:${field("scale").value} · ${field("thickness_mm").value} mm · ${field("n_boards").value} boards`;
    case "nest": return `${field("sheet_width_mm").value} × ${field("sheet_height_mm").value} mm`;
    case "label": return +field("label_density").value === 0 ? "off"
      : field("label_hidden").checked ? "hidden" : "visible";
    case "engrave": {
      const n = document.querySelectorAll(".hatch-card").length;
      return n ? `${n} layer${n > 1 ? "s" : ""}` : "";
    }
    case "export": {
      const m = /(\d+)\s*sheets/.exec($("stats").textContent);
      return $("stats").hidden || !m ? "" : `${m[1]} sheet${m[1] === "1" ? "" : "s"}`;
    }
  }
  return "";
}

function sync() {
  for (const s of PANELS) {
    const el = $(`b-${s}`);
    const text = badgeText(s);
    if (el && el.textContent !== text) el.textContent = text;   // write on change only
  }
  // the readings window shows only when there is something to read
  const empty = $("stats").hidden && $("warnings").hidden;
  if ($("readout").hidden !== empty) $("readout").hidden = empty;
}

/* ------------------------------------------------------ the floating window */

function panelState() {
  try { return JSON.parse(localStorage.getItem(PANEL_KEY) || "null") || {}; } catch { return {}; }
}
function persistPanel(patch) {
  try { localStorage.setItem(PANEL_KEY, JSON.stringify({ ...panelState(), ...patch })); } catch { /* */ }
}
// keep a grabbable corner of the header on screen, whatever the window size
const clamp = (x, y, w, h) => [Math.max(0, Math.min(innerWidth - w, x)), Math.max(0, Math.min(innerHeight - h, y))];

// fold and unfold are one control at one point: the chip lands on the button
function setFolded(min) {
  if (min) {
    const r = fold.getBoundingClientRect();   // a hidden button measures 0 x 0
    if (r.width > 0) {
      const [x, y] = clamp(r.left, r.top, 30, 26);
      chip.style.left = `${x}px`;
      chip.style.top = `${y}px`;
      persistPanel({ chipX: x, chipY: y });
    }
  }
  menu.classList.toggle("min", min);
  $("readout").classList.toggle("min", min);   // the readings fold with the menu
  fold.hidden = min;
  chip.hidden = !min;
  persistPanel({ min });
  announce();
}
// the menu's footprint changed - app.js lays the sheets out around it
const announce = () => document.dispatchEvent(new CustomEvent("dl-menu-layout"));

{
  const s = panelState();
  if (Number.isFinite(s.x) && Number.isFinite(s.y)) {
    const [x, y] = clamp(s.x, s.y, 80, 40);
    menu.style.left = `${x}px`;
    menu.style.top = `${y}px`;
  }
  if (Number.isFinite(s.chipX) && Number.isFinite(s.chipY)) {
    const [x, y] = clamp(s.chipX, s.chipY, 30, 26);
    chip.style.left = `${x}px`;
    chip.style.top = `${y}px`;
  }
  setFolded(s.min === true);
}
fold.addEventListener("click", () => setFolded(true));
chip.addEventListener("click", () => setFolded(false));
// Esc folds and unfolds - but not while typing in a field (the target is the
// document itself when nothing has focus, which has no closest())
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !e.target.closest?.("input, select, textarea")) setFolded(!menu.classList.contains("min"));
});

// drag by the header; a press on the logo or title link stays a click
{
  const head = $("menu-head");
  let drag = null;
  head.addEventListener("pointerdown", (e) => {
    if (e.target.closest("a")) return;
    drag = { dx: e.clientX - menu.offsetLeft, dy: e.clientY - menu.offsetTop };
    menu.classList.add("dragging");
    try { head.setPointerCapture(e.pointerId); } catch { /* synthetic pointer */ }
  });
  head.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const [x, y] = clamp(e.clientX - drag.dx, e.clientY - drag.dy, 80, 40);
    menu.style.left = `${x}px`;
    menu.style.top = `${y}px`;
  });
  head.addEventListener("pointerup", () => {
    if (!drag) return;
    drag = null;
    menu.classList.remove("dragging");
    persistPanel({ x: menu.offsetLeft, y: menu.offsetTop });
    announce();
  });
}

// the controls change class, hidden, rows and text for their own reasons -
// those mutations, and typing in the form, are the signal to re-read
const watch = { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["hidden"] };
new MutationObserver(sync).observe(menu, watch);
new MutationObserver(sync).observe($("readout"), watch);
$("params").addEventListener("input", sync);
sync();
