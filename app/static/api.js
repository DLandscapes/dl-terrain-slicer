"use strict";

/* Transport adapter.
 *
 * app.js never calls fetch() directly - it goes through `api`, which has two
 * implementations behind the same promise-based interface:
 *
 *   server mode  - the local FastAPI app (start.bat / the desktop download)
 *   wasm mode    - everything runs in the visitor's browser: worker.js boots
 *                  Pyodide and calls the very same Python code (app/core.py)
 *
 * build.py injects `window.TS_MODE = "wasm"` into the hosted page; without it
 * the server implementation is used. Keeping the switch in this one file is
 * what lets a single app.js serve both builds.
 */

const TS_WASM = typeof window !== "undefined" && window.TS_MODE === "wasm";

/* ---------- limits for the browser build ----------
 * Everything runs in the visitor's tab, so a raster that a desktop machine
 * shrugs off can take the page down. Guard on file size AND on pixel count:
 * a small compressed TIFF can expand to gigabytes.
 */
const LIMITS = {
  warnBytes: 50 * 1024 * 1024,
  maxBytes: 200 * 1024 * 1024,
  warnPixels: 20e6,
  maxPixels: 60e6,
};

function mb(n) {
  return (n / (1024 * 1024)).toFixed(0) + " MB";
}

/** Width/height straight from the TIFF header, without decoding the image.
 *  Returns null for anything that is not a classic TIFF we understand. */
function tiffDimensions(buf) {
  try {
    const dv = new DataView(buf);
    if (dv.byteLength < 8) return null;
    const bo = dv.getUint16(0, false);
    let le;
    if (bo === 0x4949) le = true;
    else if (bo === 0x4d4d) le = false;
    else return null;
    const magic = dv.getUint16(2, le);
    if (magic === 43) return null;      // BigTIFF - size guard has to carry it
    if (magic !== 42) return null;
    let off = dv.getUint32(4, le);
    if (off <= 0 || off + 2 > dv.byteLength) return null;
    const n = dv.getUint16(off, le);
    let w = null, h = null;
    for (let i = 0; i < n; i++) {
      const e = off + 2 + i * 12;
      if (e + 12 > dv.byteLength) break;
      const tag = dv.getUint16(e, le);
      const type = dv.getUint16(e + 2, le);
      if (tag !== 256 && tag !== 257) continue;
      const v = type === 3 ? dv.getUint16(e + 8, le) : dv.getUint32(e + 8, le);
      if (tag === 256) w = v; else h = v;
    }
    return w && h ? { width: w, height: h } : null;
  } catch {
    return null;
  }
}

/** Throws on a hard limit, returns a warning string (or "") on a soft one. */
function checkTerrainFile(name, buf) {
  // OBJ was refused here until build 22, when the per-triangle rasterizer in
  // slicer/meshload.py was vectorised (~37x): a 500k-face mesh now samples in
  // ~0.6 s native, a few seconds in WebAssembly. The generic size limits
  // below carry the guard duty for meshes too.
  const size = buf.byteLength;
  if (size >= LIMITS.maxBytes) {
    throw new Error(
      `This file is ${mb(size)}. The browser version is limited to ` +
      `${mb(LIMITS.maxBytes)} because everything runs inside this tab - ` +
      `please use the desktop version, or clip/downsample the raster in GIS.`);
  }
  const dim = tiffDimensions(buf);
  if (dim) {
    const px = dim.width * dim.height;
    if (px >= LIMITS.maxPixels) {
      throw new Error(
        `This raster is ${dim.width} x ${dim.height} cells ` +
        `(${(px / 1e6).toFixed(0)} million). The browser version stops at ` +
        `${(LIMITS.maxPixels / 1e6).toFixed(0)} million because the whole ` +
        `grid has to fit in the tab's memory - please use the desktop ` +
        `version, or resample the raster to a coarser cell size in GIS.`);
    }
    if (px >= LIMITS.warnPixels) {
      return `Large raster (${dim.width} x ${dim.height} cells) - reading it ` +
        `in the browser may take a while and needs a lot of memory.`;
    }
  }
  if (size >= LIMITS.warnBytes) {
    return `Large file (${mb(size)}) - reading it in the browser may take ` +
      `a while and needs a lot of memory.`;
  }
  return "";
}

/* ---------- server mode ---------- */
async function asJson(res) {
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* keep */ }
    throw new Error(detail);
  }
  return res.json();
}

function serverApi() {
  const form = (file) => {
    const fd = new FormData();
    fd.append("file", file);
    return fd;
  };
  return {
    mode: "server",
    onProgress: null,
    ready: async () => {},
    version: async () => asJson(await fetch("api/version")),
    demo: async () => asJson(await fetch("api/demo", { method: "POST" })),
    upload: async (file) =>
      asJson(await fetch("api/upload", { method: "POST", body: form(file) })),
    hatchAdd: async (uploadId, file) =>
      asJson(await fetch(`api/hatch?upload_id=${encodeURIComponent(uploadId)}`,
        { method: "POST", body: form(file) })),
    hatchRemove: async (uploadId, hatchId) =>
      asJson(await fetch(`api/hatch/${uploadId}/${hatchId}`, { method: "DELETE" })),
    slice: async (uploadId, params) =>
      asJson(await fetch("api/slice", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ upload_id: uploadId, params }),
      })),
    export: async (uploadId, params) => {
      const res = await fetch("api/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ upload_id: uploadId, params }),
      });
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch { /* keep */ }
        throw new Error(detail);
      }
      const cd = res.headers.get("Content-Disposition") || "";
      return {
        blob: await res.blob(),
        filename: cd.match(/filename="(.+)"/)?.[1] || "laser.zip",
      };
    },
  };
}

/* ---------- wasm mode ---------- */
function wasmApi() {
  // the ?v= is passed on to slicer.zip/manifest.json inside the worker, so a
  // new build never runs against a cached engine
  const v = window.TS_ASSET_V ? `?v=${window.TS_ASSET_V}` : "";
  // module worker: Pyodide will not start in a classic one
  const worker = new Worker(`static/worker.js${v}`, { type: "module" });
  const pending = new Map();
  let nextId = 1;
  let booted = null;

  const self_ = {
    mode: "wasm",
    onProgress: null,
  };

  worker.onmessage = (ev) => {
    const m = ev.data;
    if (m.type === "progress") {
      if (self_.onProgress) self_.onProgress(m.step, m.detail || "");
      return;
    }
    const p = pending.get(m.id);
    if (!p) return;
    pending.delete(m.id);
    if (m.ok) p.resolve(m.result);
    else p.reject(new Error(m.error || "the browser engine failed"));
  };
  worker.onerror = (ev) => {
    const err = new Error("engine failed to start: " + (ev.message || "unknown error"));
    for (const p of pending.values()) p.reject(err);
    pending.clear();
  };

  function call(op, payload, transfer) {
    return new Promise((resolve, reject) => {
      const id = nextId++;
      pending.set(id, { resolve, reject });
      worker.postMessage({ id, op, payload }, transfer || []);
    });
  }

  self_.ready = () => {
    if (!booted) booted = call("init", {});
    return booted;
  };

  const withReady = (fn) => async (...args) => {
    await self_.ready();
    return fn(...args);
  };

  self_.version = withReady(() => call("version", {}));
  self_.demo = withReady(() => call("demo", {}));

  self_.upload = withReady(async (file) => {
    const buf = await file.arrayBuffer();
    const warning = checkTerrainFile(file.name, buf);
    const res = await call("upload", { name: file.name, data: buf }, [buf]);
    if (warning) res.warnings = [warning, ...(res.warnings || [])];
    return res;
  });

  self_.hatchAdd = withReady(async (uploadId, file) => {
    const buf = await file.arrayBuffer();
    return call("hatchAdd",
      { upload_id: uploadId, name: file.name, data: buf }, [buf]);
  });

  self_.hatchRemove = withReady((uploadId, hatchId) =>
    call("hatchRemove", { upload_id: uploadId, hatch_id: hatchId }));

  self_.slice = withReady((uploadId, params) =>
    call("slice", { upload_id: uploadId, params }));

  self_.export = withReady(async (uploadId, params) => {
    const r = await call("export", { upload_id: uploadId, params });
    return {
      blob: new Blob([r.data], { type: "application/zip" }),
      filename: r.filename,
    };
  });

  return self_;
}

const api = TS_WASM ? wasmApi() : serverApi();

/* Mesh input is a CAPABILITY the UI reads, not a transport fact - app.js
 * never learns which backend it got. It was false in wasm mode until build 22
 * vectorised the rasteriser in slicer/meshload.py; both builds accept OBJ
 * now, and the flag stays so a future constrained transport can opt out
 * again without touching app.js. */
api.supportsMesh = true;
