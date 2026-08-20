import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

// Where `npm run dev` sends API calls. The console asks its own origin (see
// $lib/apiBase.js), which under the dev server is localhost:5173 — so the dev
// server has to forward the API for it. Same-origin through the proxy means no
// CORS change on the backend and no build-time API URL: the shipped bundle
// still just talks to whatever origin served it.
const API = process.env.PHARMACY_API ?? 'http://localhost:8091';

// Every prefix the backend actually serves, read off its own OpenAPI document
// rather than guessed. `/embed` is spelled out as `/embed/preview` on purpose:
// the console has a PAGE at /embed, and proxying the whole prefix would send
// the reader to the backend instead of the page.
//
// `/` is deliberately absent — the backend answers it, but the console never
// calls it and the dev server needs the root for its own home page.
const API_PREFIXES = ['/admin', '/api', '/auth', '/brand', '/health', '/ready', '/metrics', '/embed/preview'];

const proxy = Object.fromEntries(
  API_PREFIXES.map((p) => [p, { target: API, changeOrigin: true }])
);

// `/version` is the one genuine collision: the backend serves the version JSON
// there AND the console has a Version page at the same path. They are told
// apart by what is asking. A browser NAVIGATION sends `Accept: text/html`; the
// console's own fetch (via getJSON) does not. `bypass` returning a path serves
// it locally, and returning nothing lets the proxy have it.
//
// This is narrow on purpose. It applies to one path, and the discriminator is a
// header the browser sets itself — not something a caller has to remember.
proxy['/version'] = {
  target: API,
  changeOrigin: true,
  bypass(req) {
    if ((req.headers.accept || '').includes('text/html')) return '/index.html';
  }
};

export default defineConfig({
  plugins: [tailwindcss(), sveltekit()],
  server: {
    port: 5173,
    strictPort: true,
    proxy
  }
});
