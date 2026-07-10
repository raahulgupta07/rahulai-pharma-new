// Where the admin UI sends its API calls.
//
// The admin build is served BY the backend it talks to (FastAPI mounts it at
// /admin), so same-origin is always correct at runtime. Hardcoding a port meant
// the copy served from :8091 drove the :8088 backend — the two stacks silently
// shared one API, and nothing served from the optimize stack ever hit it.
//
// SSR/prerender has no `window`; the fallback is only ever used at build time.
export const API_BASE =
  typeof window !== 'undefined' && window.location.origin.startsWith('http')
    ? window.location.origin
    : 'http://localhost:8088';
