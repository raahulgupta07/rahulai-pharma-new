// Old `?tab=` links, kept working.
//
// Analytics used to be one page with fourteen sections behind six group tabs.
// The sections are pages now, so `/analytics?tab=cost` names a section that
// this page no longer draws. Rather than silently show Health & usage — which
// is exactly the failure the split was made to fix — the old id is mapped to
// the page that draws it.
//
// This is a static-adapter SPA (`ssr` and `prerender` both false in
// +layout.js), so there is no server to answer with a 3xx: the load runs in the
// browser and SvelteKit's `redirect` performs a client-side navigation. `base`
// is '/admin' — a target without it lands outside the app.
import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';
import { SECTION_ROUTE } from '$lib/analytics/routes.js';

/**
 * Every id that has ever been a `?tab=` value here — the ten original section
 * tabs, the six group ids that replaced them, and the four Activity tabs that
 * were folded in — mapped to the SECTION it meant. Sections this page still
 * draws are absent: they need no redirect.
 */
const SECTION_OF = {
  // group ids
  conversations: 'questions',
  speed: 'performance',
  delivery: 'embeds',
  activity: 'feed',
  explore: 'explore',
  // section ids that moved off this page
  questions: 'questions',
  users: 'users',
  quality: 'quality',
  diagnostics: 'diagnostics',
  cost: 'cost',
  embeds: 'embeds',
  feed: 'feed',
  audit: 'audit',
  trends: 'trends'
};

export function load({ url }) {
  const section = SECTION_OF[url.searchParams.get('tab')];
  if (!section) return;

  const [path, preset] = SECTION_ROUTE[section].split('?');
  const p = new URLSearchParams(url.searchParams);
  p.delete('tab');
  for (const [k, v] of new URLSearchParams(preset ?? '')) p.set(k, v);
  // Land ON the section rather than at the top of the page that draws it.
  if (!p.get('sec')) p.set('sec', section);
  redirect(307, `${base}${path}?${p.toString()}`);
}
