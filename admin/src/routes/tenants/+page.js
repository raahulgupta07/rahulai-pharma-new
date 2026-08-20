// /tenants is a tab of People & access now.
//
// The old URL is bookmarked, so it redirects rather than 404s. This is a
// static-adapter SPA (`ssr` and `prerender` both false in +layout.js), so there
// is no server to answer with a 3xx: the load runs in the browser and
// SvelteKit's `redirect` performs a client-side navigation. `base` is '/admin'
// — a redirect target without it lands outside the app.
import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';

export function load() {
  redirect(307, base + '/users?tab=tenants');
}
