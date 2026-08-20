import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

// The console is served BY the backend at /admin in production, so the built
// app carries `base: '/admin'` and every internal link is written against it.
//
// The dev server cannot use that base. The API also lives under /admin — 84 of
// the backend's routes are `/admin/...` — so a dev server mounted there would
// answer `/admin/analytics/summary` with the SPA's HTML fallback and the whole
// console would look like it was talking to a broken backend. Mounting the dev
// app at the root frees `/admin` for the proxy in vite.config.js, which is what
// makes `npm run dev` talk to the real API on :8091.
//
// `vite dev` sets NODE_ENV=development and `vite build` sets production, so the
// SHIPPED build is unaffected by this: it always gets '/admin'.
const dev = process.env.NODE_ENV === 'development';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    adapter: adapter({
      fallback: 'index.html'
    }),
    paths: {
      base: dev ? '' : '/admin'
    }
  }
};

export default config;
