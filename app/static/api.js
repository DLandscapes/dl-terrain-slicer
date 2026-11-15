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

/** Width/height from an ESRI ASCII grid's header (ncols, nrows), or null. */
function ascDimensions(buf) {
  const head = new TextDecoder("latin1").decode(buf.slice(0, 1024));
  const w = /^\s*ncols\s+(\d+)/im.exec(head), h = /^\s*nrows\s+(\d+)/im.exec(head);
  return w && h ? { width: +w[1], height: +h[1] } : null;
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
  const dim = tiffDimensions(buf) || ascDimensions(buf);
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
    // Declared so app.js can assign it uniformly; the desktop transport has no
    // channel for messages mid-request, so it never fires and the indicator
    // falls back to counting up. A capability, not a transport check.
    onSliceProgress: null,
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
/* The website can refuse the engine's files: the host limits static downloads
 * per connection and answers HTTP 429 (measured 2026-11-14). A refused
 * worker.js or pyodide.mjs fires a bare error event, a refused
 * pyodide.asm.wasm leaves Pyodide waiting for ever. So a boot that fails or
 * stalls is thrown away and started again in a fresh worker; files that did
 * arrive come from the browser cache on the next try. */
const RETRY_S = [5, 15, 45];  // pauses between the 4 attempts, x0.5-1.5 jitter: a
                              // classroom behind one address must not retry in step
const FIRST_S = 45;           // until the worker reports its first step
const STEP_S = 120;           // per step after that, doubled once a boot stalled:
                              // the 6.3 MB runtime on a slow shared line

function wasmApi() {
  // the ?v= is passed on to slicer.zip inside the worker, so a new build never
  // runs against a cached engine
  const v = window.TS_ASSET_V ? `?v=${window.TS_ASSET_V}` : "";
  const pending = new Map();
  let nextId = 1;
  let worker = null;
  let booted = null;
  let onBoot = null;  // while booting: called with what went wrong, or null on progress

  const self_ = {
    mode: "wasm",
    onProgress: null,
    onSliceProgress: null,   // (done, total) while a slice runs; wasm only
  };
  const progress = (step, detail) => { if (self_.onProgress) self_.onProgress(step, detail); };

  function spawn() {
    // module worker: Pyodide will not start in a classic one
    worker = new Worker(`static/worker.js${v}`, { type: "module" });
    worker.onmessage = (ev) => {
      const m = ev.data;
      if (m.type === "fetchfail") {
        if (onBoot) onBoot({ status: m.status, url: m.url });
        return;
      }
      if (m.type === "progress") {
        // step "slice" carries done/total from inside the slicing loop; every
        // other step is boot progress and carries a detail string. Exposed as a
        // CAPABILITY (onSliceProgress may simply never fire) rather than
        // something app.js has to ask the transport about - the desktop build
        // has no channel for mid-call messages and the UI falls back on its own.
        if (m.step === "slice") {
          if (self_.onSliceProgress) self_.onSliceProgress(m.done, m.total);
          return;
        }
        if (onBoot) onBoot(null);
        progress(m.step, m.detail || "");
        return;
      }
      const p = pending.get(m.id);
      if (!p) return;
      pending.delete(m.id);
      if (m.ok) p.resolve(m.result);
      else p.reject(new Error(m.error || "the browser engine failed"));
    };
    worker.onerror = (ev) => {
      if (onBoot) onBoot({ message: ev.message });
      const err = new Error(ev.message || "the browser engine stopped");
      for (const p of pending.values()) p.reject(err);
      pending.clear();
    };
  }

  function call(op, payload, transfer) {
    return new Promise((resolve, reject) => {
      const id = nextId++;
      pending.set(id, { resolve, reject });
      worker.postMessage({ id, op, payload }, transfer || []);
    });
  }

  // One attempt: resolves to null once the engine is up, else to what went wrong.
  function bootOnce(patience) {
    return new Promise((resolve) => {
      let timer = null;
      const arm = (s) => {
        clearTimeout(timer);
        timer = setTimeout(() => done({ stall: s }), s * 1000);
      };
      const done = (failure) => {
        if (!onBoot) return;  // settled already
        onBoot = null;
        clearTimeout(timer);
        resolve(failure);
      };
      onBoot = (failure) => (failure ? done(failure) : arm(patience));
      spawn();
      arm(FIRST_S);
      call("init", {}).then(() => done(null), (err) => done({ message: err.message }));
    });
  }

  // The HTTP status of the two files every boot needs first, when the failure
  // itself did not say: {status, url}, status -1 = unreachable, null = both fine.
  async function probe() {
    for (const url of [`static/worker.js${v}`, "static/pyodide/pyodide.mjs"]) {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 10000);
      try {
        const r = await fetch(url, { cache: "no-store", signal: ctl.signal });
        if (!r.ok) return { status: r.status, url };
      } catch {
        return { status: -1, url };
      } finally {
        clearTimeout(t);
      }
    }
    return null;
  }

  async function explain(f) {
    const p = f.status > 0 ? f : (await probe()) || f;
    const file = (p.url || "").split("?")[0].split("/").pop();
    if (p.status === 429) return "The website is limiting downloads from your connection right now (HTTP 429).";
    if (p.status > 0) return `Could not load ${file} (HTTP ${p.status}).`;
    if (p.status < 0) return `Could not reach ${location.hostname} - check your internet connection.`;
    if (p.url) return `Could not load ${file} (the download broke off).`;
    if (p.stall) return `The download stopped (nothing arrived for ${p.stall} s).`;
    return `The engine failed to start (${p.message || "unknown error"}).`;
  }

  async function bootWithRetry() {
    let patience = STEP_S;
    for (let attempt = 0; ; attempt++) {
      const failure = await bootOnce(patience);
      if (!failure) return;
      worker.onmessage = worker.onerror = null;
      worker.terminate();
      pending.clear();
      if (failure.stall) patience = 2 * STEP_S;
      const why = await explain(failure);
      if (attempt >= RETRY_S.length) throw new Error(why);
      const wait = Math.round(RETRY_S[attempt] * (0.5 + Math.random()));
      progress("retry", `${why} Retrying in ${wait} s…`);
      await new Promise((r) => setTimeout(r, wait * 1000));
      progress("start", `trying again (attempt ${attempt + 2} of ${RETRY_S.length + 1})…`);
    }
  }

  self_.ready = () => {
    if (!booted) booted = bootWithRetry();
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
