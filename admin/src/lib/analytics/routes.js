// Which page draws which analytics section.
//
// This map exists because the sections used to be TABS on one page, and the
// links between them set `?tab=<section id>`. When the ten tabs were grouped
// into six, the tab ids changed (`questions` became a section INSIDE the
// `conversations` group) and every one of the forty-six links that named a section was
// left naming it. `tab` falls back to `overview` when it does not recognise the
// id, so every one of those links navigated, applied its filter, and drew the
// WRONG panel — a chart click that looks like it worked.
//
// Naming the destination by SECTION rather than by page is what stops that
// happening again: a section can move to another page and every link that
// points at it follows, because they all resolve through here.

/** section id → the route that draws it, with any tab it needs. */
export const SECTION_ROUTE = {
  overview: '/analytics',
  performance: '/analytics',
  cache: '/analytics',
  health: '/analytics',

  questions: '/conversations',
  users: '/conversations',

  quality: '/quality?tab=answers',
  diagnostics: '/quality?tab=diagnostics',

  cost: '/cost',

  embeds: '/embed?tab=analytics',

  feed: '/activity',
  audit: '/activity',
  trends: '/activity',
  explore: '/activity'
};

/**
 * The sections read from `/admin/activity/*` rather than from the chat-turn
 * endpoints. They carry a different filter bar — events have a source, an
 * actor and an action; turns have a store, a language and a model — so a page
 * never mixes the two. `ACTIVITY_SECTIONS` is what a page checks to know which
 * bar it is.
 */
export const ACTIVITY_SECTIONS = new Set(['feed', 'audit', 'trends', 'explore']);

/** Filter keys that belong to the chat-turn bar (the API's own names). */
export const FILTER_KEYS = [
  'store', 'lang', 'path', 'embed', 'model', 'actor', 'cached', 'rated', 'q', 'tool'
];

/** Filter keys that belong to the event bar. */
export const ACTIVITY_KEYS = ['source', 'actor', 'action', 'from', 'to', 'q'];

/**
 * Parameters that describe a position INSIDE one panel — a selected day, an
 * open drawer, a chosen measure. They are dropped when crossing to another
 * page: a stale `measure=spend` in the URL of a page with no such control is a
 * trap for whoever copies the link.
 */
export const PANEL_LOCAL = [
  'day', 'issue', 'sec', 'sub', 'measure', 'by', 'rollup', 'top', 'offset',
  'compare', 'metric', 'turn', 'trace'
];

/**
 * Build the href for a section, carrying the filters that still apply.
 *
 * `from` is the current URL. Filters cross only between pages that read the
 * same endpoints: `actor` and `q` are spelled the same in both bars and mean
 * different things, so carrying them across the divide would silently narrow
 * the destination by a string the reader never typed there.
 */
export function hrefFor(section, from, mutate) {
  const target = SECTION_ROUTE[section];
  if (!target) throw new Error(`no route draws the "${section}" section`);
  const [path, preset] = target.split('?');

  const p = new URLSearchParams(preset ?? '');
  const crossing = ACTIVITY_SECTIONS.has(section) !== isActivityUrl(from);
  if (!crossing && from) {
    const keep = ACTIVITY_SECTIONS.has(section) ? ACTIVITY_KEYS : FILTER_KEYS;
    for (const k of [...keep, 'range']) {
      const v = from.searchParams.get(k);
      if (v) p.set(k, v);
    }
  }
  // Where to land inside the destination. A page that draws one section has no
  // use for it and ignores it.
  p.set('sec', section);
  mutate?.(p);
  const qs = p.toString();
  return qs ? `${path}?${qs}` : path;
}

/**
 * Does this URL belong to a page that reads the event endpoints? Derived from
 * the pathname, because that is the only thing that survives a navigation.
 */
export function isActivityUrl(u) {
  if (!u) return false;
  return /\/(activity|security-log)(\/|$|\?)/.test(u.pathname + '?');
}
