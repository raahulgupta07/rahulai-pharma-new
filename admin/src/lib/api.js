// API client for the CitCare pharmacy agent backend.
// Same-origin: the backend serves this build. See apiBase.js.

import { API_BASE } from './apiBase.js';

export const base = API_BASE;

/**
 * Error carrying the HTTP status that caused it.
 *
 * `status === 0` means the request never reached a server (DNS, refused
 * connection, CORS). Anything else means the backend answered — which is why
 * pages must not print "backend offline" for it. See $lib/ErrorState.svelte.
 */
export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/**
 * GET a JSON endpoint, throwing ApiError (with the status) on failure.
 * The shared entry point for page loaders — use this rather than a bare
 * fetch, so the failure panel can tell 401 from a dead backend.
 */
export async function getJSON(path, init = {}) {
  let res;
  try {
    res = await fetch(base + path, init);
  } catch (e) {
    throw new ApiError(0, e?.message || 'network error');
  }
  if (!res.ok) {
    let detail = '';
    try {
      const j = JSON.parse(await res.text());
      detail = typeof j?.detail === 'string' ? j.detail : '';
    } catch {
      /* non-JSON error body — the status alone is the message */
    }
    throw new ApiError(res.status, detail || `HTTP ${res.status}`);
  }
  const text = await res.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return {};
  }
}

/**
 * Internal fetch wrapper. Returns parsed JSON on success.
 * Throws an ApiError carrying the status so callers can distinguish an
 * expired session from a stopped backend.
 */
async function request(path, { method = 'GET', body } = {}) {
  let res;
  try {
    res = await fetch(base + path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined
    });
  } catch (e) {
    throw new ApiError(0, e?.message || 'network error');
  }

  if (!res.ok) {
    throw new ApiError(res.status, `HTTP ${res.status}`);
  }

  // Some endpoints may return empty bodies; guard JSON parsing.
  const text = await res.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return {};
  }
}

export function getMetrics() {
  return request('/metrics');
}

export function getReady() {
  return request('/ready');
}

export function reload() {
  return request('/api/embed/reload', { method: 'POST' });
}

export function ingest() {
  return request('/api/embed/ingest', { method: 'POST' });
}
