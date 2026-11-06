/**
 * Native macOS pickers, exposed by the WKWebView shell as the `hlcPick`
 * message handler. Returns a promise of an array of paths, or null when the
 * app runs in a plain browser (callers then show a typed-path fallback).
 */
let sequence = 0;
const pending = new Map();

window.hlcPickResult = (payload) => {
  const id = payload?.id;
  const paths = Array.isArray(payload?.paths) ? payload.paths : [];
  const resolve = pending.get(id);
  if (!resolve) return;
  pending.delete(id);
  resolve(paths);
};

function handler() {
  return window.webkit?.messageHandlers?.hlcPick || null;
}

export function hasNativePicker() {
  return Boolean(handler());
}

export function nativePick({
  mode = 'open',
  directories = false,
  multiple = false,
  extensions = [],
  filename = '',
} = {}) {
  const picker = handler();
  if (!picker) return Promise.resolve(null);
  const id = `pick-${Date.now()}-${(sequence += 1)}`;
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      resolve([]);
    }, 120000);
    pending.set(id, (paths) => {
      clearTimeout(timer);
      resolve(paths);
    });
    picker.postMessage({ id, mode, directories, multiple, extensions, filename });
  });
}

export function openTerminal(command) {
  const handler = window.webkit?.messageHandlers?.hlcTerminal || null;
  if (!handler || !command) return false;
  handler.postMessage({ command });
  return true;
}
