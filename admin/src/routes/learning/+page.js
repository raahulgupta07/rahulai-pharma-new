// /learning was merged into the Learning tab of Quality.
//
// The old URL is bookmarked, so it redirects rather than 404s. This is a
// static-adapter SPA (`adapter-static` with an index.html fallback, `ssr` and
// `prerender` both false in +layout.js), so there is no server to answer with a
// 3xx; the load runs in the browser and SvelteKit's `redirect` performs a
// client-side navigation, which is the only form that can work here.
//
// `base` is '/admin' — the app is not served from the domain root, and a
// redirect target without it lands outside the app.
import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';

export function load() {
  redirect(307, base + '/quality?tab=learning');
}
