/* DL Terrain Slicer - browser build worker.
 *
 * An ES module worker: current Pyodide refuses to start in a classic worker
 * ("Classic web workers are not supported"), so this is loaded with
 * {type: "module"} and pulls the runtime in with a static import.
 *
 * Boots Pyodide, loads the numeric stack and the slicer package, then serves
 * the same operations the FastAPI app serves - by calling the very same
 * Python code (app/core.py via bridge.py). Nothing leaves the machine: every
 * file the user picks is read here, in this tab.
 *
 * Everything is loaded from this app's own directory - no CDN, so no third
 * party ever sees a visitor's IP address.
 */

import { loadPyodide } from "./pyodide/pyodide.mjs";

// Every download the engine makes - Pyodide's own included, it calls the global
// fetch - reports a refusal at once, with file and status: a refused
// pyodide.asm.wasm would otherwise leave Pyodide waiting for ever, and a refused
// package would only surface later as "No module named ...". api.js decides
// what to do about it (start again, or explain).
const realFetch = self.fetch.bind(self);
self.fetch = async (input, init) => {
  const url = String((input && input.url) || input);
  try {
    const res = await realFetch(input, init);
    if (!res.ok) self.postMessage({ type: "fetchfail", url, status: res.status });
    return res;
  } catch (err) {
    self.postMessage({ type: "fetchfail", url, status: 0 });
    throw err;
  }
};

// must equal NEEDED_PACKAGES in web/fetch_pyodide.py (native code, from the
// pinned Pyodide); the pure-Python wheels travel inside slicer.zip
const PACKAGES = ["numpy", "contourpy", "shapely"];

const V = new URLSearchParams(self.location.search).get("v") || "";
const rel = (path) => new URL(path + (V ? (path.includes("?") ? "&" : "?") + "v=" + V : ""),
  self.location).href;
const bare = (path) => new URL(path, self.location).href;

let pyodide = null;
let bridge = null;
let booting = null;

function progress(step, detail) {
  self.postMessage({ type: "progress", step, detail });
}

async function boot() {
  progress("runtime", "loading the Python runtime…");
  pyodide = await loadPyodide({
    indexURL: bare("pyodide/"),
    stdout: (line) => console.log("[py]", line),
    stderr: (line) => console.warn("[py]", line),
  });

  progress("packages", "loading numpy, shapely, contourpy…");
  await pyodide.loadPackage(PACKAGES);

  progress("slicer", "loading the slicer, tifffile, ezdxf…");
  const zip = await (await fetch(rel("slicer.zip"))).arrayBuffer();
  pyodide.unpackArchive(zip, "zip");

  bridge = pyodide.runPython("import bridge; bridge");
  progress("ready", "ready");
}

/** bytes coming back from Python, as a plain Uint8Array. */
function toBytes(value) {
  if (value instanceof Uint8Array) return value;
  if (value && typeof value.toJs === "function") {
    const out = value.toJs();
    value.destroy();
    return out instanceof Uint8Array ? out : new Uint8Array(out);
  }
  const buf = value.getBuffer("u8");
  const copy = buf.data.slice();
  buf.release();
  value.destroy();
  return copy;
}

/** bridge.* returns a JSON envelope: unwrap it or throw the message. */
function unwrap(jsonText) {
  const env = JSON.parse(jsonText);
  if (!env.ok) throw new Error(env.error);
  return env.result;
}

const ops = {
  init: async () => ({ build: unwrap(bridge.version()).build }),
  version: async () => unwrap(bridge.version()),
  demo: async () => unwrap(bridge.demo()),
  upload: async (p) => unwrap(bridge.upload(p.name, new Uint8Array(p.data))),
  hatchAdd: async (p) =>
    unwrap(bridge.hatch_add(p.upload_id, p.name, new Uint8Array(p.data))),
  hatchRemove: async (p) => unwrap(bridge.hatch_remove(p.upload_id, p.hatch_id)),
  slice: async (p) => unwrap(bridge.do_slice(JSON.stringify(p))),
  export: async (p) => {
    const meta = unwrap(bridge.do_export(JSON.stringify(p)));
    return { filename: meta.filename, data: toBytes(bridge.take_export()) };
  },
};

self.onmessage = async (ev) => {
  const { id, op, payload } = ev.data;
  try {
    if (!booting) booting = boot();
    await booting;
    const handler = ops[op];
    if (!handler) throw new Error("unknown operation: " + op);
    const result = await handler(payload || {});
    const transfer = result && result.data instanceof Uint8Array
      ? [result.data.buffer] : [];
    self.postMessage({ id, ok: true, result }, transfer);
  } catch (err) {
    self.postMessage({ id, ok: false, error: (err && err.message) || String(err) });
  }
};
