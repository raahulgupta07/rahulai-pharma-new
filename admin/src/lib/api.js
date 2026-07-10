// API client for the CitCare pharmacy agent backend.
// Same-origin: the backend serves this build. See apiBase.js.

import { API_BASE } from './apiBase.js';

export const base = API_BASE;

/**
 * Internal fetch wrapper. Returns parsed JSON on success.
 * Throws an Error with a readable message on failure so callers
 * can show graceful "backend offline" style states.
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
    throw new Error('backend offline');
  }

  if (!res.ok) {
    throw new Error(`request failed (${res.status})`);
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
