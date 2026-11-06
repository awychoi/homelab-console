const TOKEN = document.querySelector('meta[name="hlc-token"]')?.content || '';

export class ApiError extends Error {
  constructor(status, message, payload) {
    super(message || `HTTP ${status}`);
    this.status = status;
    this.payload = payload || {};
  }
}

async function request(method, path, body) {
  const headers = { 'X-HLC-Token': TOKEN };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: 'no-store',
    });
  } catch (error) {
    throw new ApiError(0, 'The console server is not reachable.', { network: true });
  }
  const text = await response.text();
  let payload = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = { error: text.slice(0, 500) }; }
  }
  if (!response.ok) {
    if (response.status === 401) {
      window.dispatchEvent(new CustomEvent('hlc:unauthorized'));
    }
    throw new ApiError(response.status, payload?.error || `HTTP ${response.status}`, payload || {});
  }
  return payload;
}

export const api = {
  token: TOKEN,
  get: (path) => request('GET', path),
  post: (path, body = {}) => request('POST', path, body),
  put: (path, body = {}) => request('PUT', path, body),
  patch: (path, body = {}) => request('PATCH', path, body),
  del: (path) => request('DELETE', path),
};

/**
 * Stream a run over SSE. Returns a close function.
 * handlers: { onLines(lines, meta), onDone(meta), onError(error), onStatus(status) }
 *
 * EventSource cannot send X-HLC-Token, so the page first asks the API for a
 * short-lived token scoped to this one run.  The full session token never
 * appears in a URL.
 */
export function streamRun(runId, handlers = {}, since = 0) {
  let source = null;
  let closed = false;
  const close = () => {
    closed = true;
    source?.close();
  };
  fetch(`/api/runs/${encodeURIComponent(runId)}/stream-token`, {
    headers: { 'X-HLC-Token': TOKEN },
    cache: 'no-store',
  })
    .then(async (response) => {
      if (!response.ok) throw new Error(`stream token HTTP ${response.status}`);
      return response.json();
    })
    .then((data) => {
      if (closed) return;
      source = new EventSource(
        `/api/runs/${encodeURIComponent(runId)}/stream?since=${since}&stream_token=${encodeURIComponent(data.token || '')}`,
      );
      source.addEventListener('lines', (event) => {
        try {
          const payload = JSON.parse(event.data);
          handlers.onStatus?.(payload.status);
          handlers.onLines?.(payload.lines || [], payload);
        } catch (error) {
          handlers.onError?.(error);
        }
      });
      source.addEventListener('done', (event) => {
        try {
          const payload = JSON.parse(event.data);
          handlers.onStatus?.(payload.status);
          handlers.onDone?.(payload);
        } catch (error) {
          handlers.onError?.(error);
        } finally {
          close();
        }
      });
      source.addEventListener('ping', () => {});
      source.onerror = () => {
        if (closed) return;
        if (source.readyState === EventSource.CLOSED) {
          handlers.onError?.(new Error('run stream closed unexpectedly'));
        }
      };
    })
    .catch((error) => {
      if (!closed) handlers.onError?.(error);
    });
  return close;
}
