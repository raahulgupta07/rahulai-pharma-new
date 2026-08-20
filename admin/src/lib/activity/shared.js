// Shared plumbing for the four Activity tabs.
//
// Three rules from the contract are enforced HERE rather than in each tab, so
// there is one place to check them:
//
//  1. ABSENT IS NOT ZERO. `kpi()` never coerces a missing number; `null` stays
//     `null` and every caller renders `—`. `Number(null)` is 0 and `x || 0`
//     turns an unknown into a measured zero — neither appears in this folder.
//  2. AN ABSENT MOVEMENT BLOCK IS NOT "NO CHANGE". `deltaOf()` returns `null`
//     when the payload carried no movement at all, so `DeltaChip` draws
//     nothing; a block that IS present with a null delta prints "no prior
//     period". Those are three different claims and only one of them is 0%.
//  3. A SECTION FAILS ALONE. `fetchSection()` returns a shape, never throws, so
//     one 404 endpoint cannot blank a tab.
//
// The endpoints these tabs read (`/admin/activity/{summary,trends,explore,audit}`)
// may not exist on the running backend at all. That is a deployment fact, not a
// fault, and it renders as "not served by this backend" — never as an empty
// chart, which would read as "nothing happened".

import { getJSON, ApiError } from '$lib/api.js';

export const UNKNOWN = '—';
export const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/**
 * These four were the tabs of a page of their own. They are now SECTIONS of two
 * groups on /analytics, and `?tab=` names the group, not the section.
 *
 * Every cross-link inside these components goes through `openSection` rather
 * than writing `{tab: 'audit'}` by hand. Writing the section name into `tab`
 * still "works" in the sense that nothing throws — the analytics page validates
 * `?tab=` against its own group ids and quietly falls back to Overview — so a
 * KPI that means "show me the sign-in failures" would land the reader on a page
 * of chat-turn charts with no error anywhere. That is the failure this map
 * exists to prevent.
 */
export const GROUP_OF_SECTION = {
  feed: 'activity',
  audit: 'activity',
  trends: 'explore',
  explore: 'explore'
};

/** Params that open one of these sections: the group in `tab`, the section in `sec`.
 *
 * `sec` and not `sub` because `sub` is ALREADY this folder's own parameter —
 * Explore's stacking subgroup — and `exploreHref` passes `sub=source` through
 * `extra`. Sharing the name let that spread overwrite the section, which is the
 * worst shape this bug has: the link navigates, the page renders, and the
 * reader is quietly on the wrong section. */
export const openSection = (id, extra = {}) => ({
  tab: GROUP_OF_SECTION[id] ?? 'activity',
  sec: id,
  ...extra
});

/** The four sections, in rail order. Kept for labels and for `TAB_KEYS`. */
export const TABS = [
  { key: 'feed', label: 'Feed' },
  { key: 'trends', label: 'Trends' },
  { key: 'explore', label: 'Explore' },
  { key: 'audit', label: 'Audit' }
];
export const TAB_KEYS = TABS.map((t) => t.key);

/** The three feeds the backend merges. A source outside this list still shows. */
export const SOURCES = [
  { key: 'app', label: 'App' },
  { key: 'auth', label: 'Auth' },
  { key: 'ingest', label: 'Ingest' }
];

/**
 * The named chart fills, every one of them a --color-series-* token.
 *
 * Three of these used to be borrowed from the semantic scale and two of the
 * borrowings were the same colour. Measured on the running console in dark
 * mode: `app` (--color-accent) and `ingest` (--color-success) both resolved to
 * rgb(155,160,240) — ratio 1.00, dE2000 0.0. Two different event sources, one
 * swatch, on every Feed chart, with nothing on screen admitting it. `auth`
 * (--color-accent-2) measured 2.24:1 against --color-surface and 2.45:1 against
 * the page: a de-emphasis colour asked to carry a category.
 *
 * `muted` is the same class of mistake in the other direction. It was
 * --color-line-2, a hairline: 1.18:1 on a card in dark, 1.14:1 in light. It is
 * used for "Blocked by IP", for "3xx", and for "not recorded" — bands a reader
 * is meant to READ. A de-emphasis colour is fine for de-emphasis and wrong for
 * a category, so it now points at --color-series-other, which is legible.
 *
 * `ok`/`bad`/`warn` keep a verdict, so they keep a verdict colour; `bad` and
 * `warn` are still --color-danger/--color-warning. `ok` is NOT --color-success,
 * because in dark mode --c-success and --c-accent are byte-identical and `ok`
 * next to `app` would be the original defect again.
 */
export const COLOR = {
  app: 'var(--color-series-1)',
  auth: 'var(--color-series-5)',
  ingest: 'var(--color-series-4)',
  ok: 'var(--color-series-3)',
  bad: 'var(--color-danger)',
  warn: 'var(--color-warning)',
  muted: 'var(--color-series-other)',
  accent: 'var(--color-series-1)'
};

/**
 * The browser's IANA zone, sent on every request and shown in the header.
 * A browser that cannot answer gets UTC — which is what the backend defaults
 * to, so the label and the buckets still agree.
 */
export function browserTz() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

/**
 * A KPI may arrive as a bare number or as the §B object
 * `{value, prev, delta, delta_pct, spark}`. Both are read; neither is invented.
 */
export function kpi(x) {
  if (isNum(x)) return { value: x, prev: null, delta: null, delta_pct: null, spark: null };
  if (!x || typeof x !== 'object')
    return { value: null, prev: null, delta: null, delta_pct: null, spark: null };
  return {
    value: isNum(x.value) ? x.value : null,
    prev: isNum(x.prev) ? x.prev : null,
    delta: isNum(x.delta) ? x.delta : null,
    delta_pct: isNum(x.delta_pct) ? x.delta_pct : null,
    spark: Array.isArray(x.spark) ? x.spark.filter((v) => v === null || isNum(v)) : null
  };
}

/** Integer with separators, or `—`. */
export const int = (v) => (isNum(v) ? Math.round(v).toLocaleString() : UNKNOWN);

/**
 * The movement block for `DeltaChip` / `Kpi delta=…`, and the distinction that
 * makes the chip honest:
 *
 *   null                     — the payload carried NO movement block. We do not
 *                              know whether it moved, so nothing is drawn.
 *   {delta: null, …}         — a block that IS present and says there is no
 *                              prior window. The chip prints "no prior period".
 *
 * Those are different claims and only one of them is "we measured no change",
 * which neither of them is. A KPI that arrives as a bare number therefore gets
 * `null` here, not a block full of nulls.
 */
export function deltaOf(x) {
  if (!x || typeof x !== 'object') return null;
  const has = ['delta', 'delta_pct', 'prev', 'prev_period'].some((k) => k in x);
  if (!has) return null;
  return {
    value: isNum(x.value) ? x.value : null,
    prev: isNum(x.prev) ? x.prev : null,
    delta: isNum(x.delta) ? x.delta : null,
    delta_pct: isNum(x.delta_pct) ? x.delta_pct : null,
    prev_period: x.prev_period ?? null
  };
}

/**
 * §5: a block that cannot honour the active filters must say so on the block,
 * not only in a banner somebody scrolled past.
 */
export function unfilteredOf(x, why = 'This number cannot honour the active filters.') {
  return x && typeof x === 'object' && x.filters_applied === false ? why : false;
}

/**
 * One section's fetch. Returns a state object and NEVER throws, so a tab keeps
 * rendering every other panel when one endpoint is missing or broken.
 */
export async function fetchSection(path) {
  try {
    const data = await getJSON(path);
    return { status: 'ok', data, err: null, path: path.split('?')[0] };
  } catch (e) {
    if (e instanceof ApiError && e.status === 404)
      return { status: 'missing', data: null, err: e, path: path.split('?')[0] };
    return { status: 'error', data: null, err: e, path: path.split('?')[0] };
  }
}

export const loadingSection = (path = '') => ({ status: 'loading', data: null, err: null, path });

/**
 * The zone the ENDPOINT says it bucketed in — never the one we sent.
 *
 * An endpoint that has not declared `tz` as a parameter does not fail: FastAPI
 * drops the unknown query param and answers 200 with UTC buckets. Reading our
 * own request back would therefore print "GMT+6:30" over UTC data, which is the
 * original defect wearing better manners. No echo means UTC, and the UI says so.
 */
export function tzEcho(data) {
  if (!data || typeof data !== 'object') return null;
  const e = data.bucket_tz ?? data.tz ?? data.timezone;
  return typeof e === 'string' && e ? e : null;
}

const isUtcZone = (z) => !z || z === 'UTC' || z === 'Etc/UTC' || z === 'GMT' || z === 'Etc/GMT';

/**
 * Warn when the buckets are cut on UTC midnight while the labels read local.
 * A reader who IS on UTC sees no discrepancy and gets no banner, and correct,
 * echoed-local data gets none either — a banner that never clears is a banner
 * nobody reads.
 */
export function shouldWarnTz(data, zone) {
  if (isUtcZone(zone)) return false;
  return isUtcZone(tzEcho(data));
}

/** Shared filter object → query string. Every tab sends the same set, plus `tz`. */
export function buildQuery(f, extra = {}) {
  const p = new URLSearchParams();
  p.set('tz', f.tz);
  if (f.from) p.set('from', f.from);
  if (f.to) p.set('to', f.to);
  if (f.source) p.set('source', f.source);
  if (f.actor) p.set('actor', f.actor);
  if (f.action) p.set('action', f.action);
  if (f.q) p.set('q', f.q);
  for (const [k, v] of Object.entries(extra)) {
    if (v !== null && v !== undefined && v !== '') p.set(k, String(v));
  }
  return p.toString();
}

/**
 * Colours for series whose keys the palette does not name.
 *
 * THE WHEEL DOES NOT TURN. It used to: six entries indexed `i % 6`, of which
 * --color-success and --color-accent were the same colour in dark mode, so a
 * ten-category stacked bar came out in FIVE distinct fills. Measured in DOM
 * order on /activity "Events by Action, daily": danger, series-1, accent-2,
 * series-1, line-2, warning, danger, series-1, accent-2, series-1. Five pairs
 * of categories sharing a swatch, and the chart said nothing about it.
 *
 * A chart that runs out of colours has three honest options and reusing one is
 * not among them: drop the tail, add a non-colour channel, or GROUP the tail.
 * `pivotRows` groups — the members past the palette become one band labelled
 * "Other (n)", so the bars still total to the whole and the count of what was
 * folded is on screen. Dropping would make the bars stop summing to the total;
 * a second channel (hatching) is a bigger change than this defect warrants when
 * the table under the chart already lists every member by name.
 *
 * `seriesColor` therefore CLAMPS instead of wrapping. An index past the end is
 * a bug in the caller, and it comes out in the fold colour — visibly the same
 * band, not a second category wearing the first one's swatch.
 */
export const SERIES = [
  'var(--color-series-1)',
  'var(--color-series-2)',
  'var(--color-series-3)',
  'var(--color-series-4)',
  'var(--color-series-5)',
  'var(--color-series-6)'
];

/** The fold band, and any de-emphasised series that still carries meaning. */
export const SERIES_OTHER = 'var(--color-series-other)';

/** How many categories this console can paint and still tell apart. */
export const SERIES_LIMIT = SERIES.length;

/** Never `SERIES[i % SERIES.length]`. See the comment above. */
export const seriesColor = (i) => (i >= 0 && i < SERIES.length ? SERIES[i] : SERIES_OTHER);

/** A dimension member the backend recorded as NULL — a real band, not a bug. */
export const NOT_RECORDED = 'not recorded';
const NULL_KEY = ' null';
/** The fold band's key.
 *
 * NUL-prefixed, exactly like NULL_KEY above, so no real dimension member can
 * collide with it: an actor genuinely called "other" keeps its own band and
 * is not silently absorbed into the fold. Written as an escape rather than as
 * a literal control character — the literal one in NULL_KEY is why `grep`
 * calls this file binary. */
export const OTHER_KEY = '\u0000other';

/**
 * Flat bucket rows — `[{t, events, app, auth, ingest, failed}]` — into the
 * `{labels, series}` the shared charts take. The backend sends one row per
 * bucket with a column per series; that is the same data in a different
 * arrangement, not different data.
 *
 * A column missing from a row stays `null`. Zero-filling is the backend's job
 * (and it does it); inventing a 0 here would turn a bucket it never sent into
 * a measured zero.
 */
export function fromRows(rows, cols, tKey = 't') {
  const list = Array.isArray(rows) ? rows : [];
  return {
    keys: list.map((r) => r?.[tKey] ?? null),
    labels: list.map((r) => bucketLabel(r?.[tKey])),
    series: cols.map((c, i) => ({
      key: c.key,
      label: c.label,
      color: c.color ?? seriesColor(i),
      area: c.area === true,
      values: list.map((r) => (isNum(r?.[c.key]) ? r[c.key] : null))
    }))
  };
}

/**
 * Long rows — `[{t, key, value}]` — into one series per distinct key.
 *
 * `key: null` is kept as its own band labelled "not recorded". Dropping it
 * would make the shares stop summing to the whole, and an event with no actor
 * is a fact about the data, not a row to hide.
 *
 * MORE MEMBERS THAN COLOURS. The palette can tell six categories apart (see
 * SERIES). Past that, the tail is summed into ONE band labelled
 * "Other (n members)" and painted in the fold colour, ranked so the band that
 * disappears is always the smallest. The alternative that shipped was to cycle
 * the wheel, which drew ten categories in five colours and told nobody. The
 * count is in the label because "Other" on its own does not say how much of the
 * chart it is standing for, and the panel's own table still lists every member
 * by name — the fold costs a reader a colour, never a number.
 */
export function pivotRows(rows, palette = {}, field = 'key') {
  const list = Array.isArray(rows) ? rows : [];
  const ts = [...new Set(list.map((r) => String(r?.t ?? '')))].sort();
  const at = new Map(ts.map((t, i) => [t, i]));
  const groups = new Map();
  for (const r of list) {
    const k = r?.[field] == null ? NULL_KEY : String(r[field]);
    if (!groups.has(k)) groups.set(k, new Array(ts.length).fill(null));
    const i = at.get(String(r?.t ?? ''));
    if (i == null || !isNum(r?.value)) continue;
    // Grouping on the SUBGROUP folds several rows into one cell, so values
    // accumulate rather than overwrite. `null` (never measured) plus a number
    // is that number; it must not stay null, and it must not start at 0 for a
    // cell nothing was ever added to.
    const cur = groups.get(k)[i];
    groups.get(k)[i] = (cur === null ? 0 : cur) + r.value;
  }

  const total = (vs) => vs.reduce((a, v) => a + (isNum(v) ? v : 0), 0);
  // A key the CALLER named a colour for is never folded away: it was asked for
  // by name, so it is not part of the anonymous tail the palette ran out for.
  const entries = [...groups.entries()];
  const named = entries.filter(([k]) => palette[k]);
  const rest = entries.filter(([k]) => !palette[k]).sort((a, b) => total(b[1]) - total(a[1]));
  const room = Math.max(0, SERIES_LIMIT - named.length);

  let kept = rest;
  let folded = [];
  if (rest.length > room) {
    // Keep one slot for the fold band itself, or a fold of exactly one member
    // would replace a named category with the word "Other".
    kept = rest.slice(0, Math.max(0, room - 1));
    folded = rest.slice(kept.length);
  }

  const series = [...named, ...kept].map(([k, values], i) => ({
    key: k,
    label: k === NULL_KEY ? NOT_RECORDED : k,
    color: palette[k] ?? seriesColor(i),
    area: false,
    values
  }));

  if (folded.length) {
    series.push({
      key: OTHER_KEY,
      label: `Other (${folded.length} member${folded.length === 1 ? '' : 's'})`,
      color: SERIES_OTHER,
      area: false,
      folded: folded.map(([k]) => (k === NULL_KEY ? NOT_RECORDED : k)),
      // Summed the same way a cell is: null is "never measured" and stays null
      // until something real is added to it, so a bucket no folded member
      // reported does not become a measured zero.
      values: ts.map((_, i) =>
        folded.reduce((acc, [, vs]) => (isNum(vs[i]) ? (acc === null ? 0 : acc) + vs[i] : acc), null)
      )
    });
  }

  return { keys: ts, labels: ts.map(bucketLabel), series };
}

/** A dimension member for display: NULL is named, never blanked or dropped. */
export const memberLabel = (k) => (k == null || k === '' ? NOT_RECORDED : String(k));

/** "11 Aug" for a bucket label; an hour bucket keeps its hour. */
export function bucketLabel(t) {
  if (!t) return UNKNOWN;
  const raw = String(t);
  const iso = raw.length === 10 ? `${raw}T00:00:00` : raw.replace(' ', 'T');
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return raw;
  if (raw.length > 10 && !/T00:00/.test(iso))
    return d.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit' });
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}
