// /branding was merged into the Branding tab of Settings.
//
// The old URL is bookmarked, so it redirects rather than 404s. This is a
// static-adapter SPA (`adapter-static` with an index.html fallback, `ssr` and
// `prerender` both false in +layout.js), so there is no server to answer with a
// 3xx; the load runs in the browser and SvelteKit's `redirect` performs a
// client-side navigation, which is the only form that can work here.
//
// The page's OWN tabs used to ride `?tab=`; inside /settings that parameter
// belongs to the outer tab bar, so an old deep link's tab is carried across as
// `?sub=` rather than dropped. An unknown value is left to the panel, which
// falls back to its first tab.
//
// `base` is '/admin' — the app is not served from the domain root, and a
// redirect target without it lands outside the app.
import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';

const SUB = ['identity', 'logos', 'parent', 'preview'];

export function load({ url }) {
  const sub = url.searchParams.get('tab');
  const carry = SUB.includes(sub) ? `&sub=${sub}` : '';
  redirect(307, `${base}/settings?tab=branding${carry}`);
}
