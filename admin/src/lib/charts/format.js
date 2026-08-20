// Formatting for the analytics page. One rule runs through all of it:
//
//   `—` means WE DO NOT KNOW. `0` means we measured zero.
//
// Every helper here returns UNKNOWN for a non-finite input rather than
// coercing it. `Number(null)` is 0 and `x || 0` turns an unknown into a
// measured zero — both are banned in this file and in its callers.

export const UNKNOWN = '—';

export const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/** Integer with thousands separators, or `—`. */
export const int = (v) => (isNum(v) ? Math.round(v).toLocaleString() : UNKNOWN);

/** Milliseconds as ms under a second, seconds above it. */
export function ms(v) {
  if (!isNum(v)) return UNKNOWN;
  if (v < 1000) return `${Math.round(v)}ms`;
  return `${(v / 1000).toFixed(1)}s`;
}

/** Seconds, for a KPI that wants the unit rendered separately. */
export function secs(v, dp = 1) {
  if (!isNum(v)) return null;
  return (v / 1000).toFixed(dp);
}

/**
 * A rate may arrive as a fraction (0.31) or already as a percentage (31).
 * Normalise to percent — without ever turning a missing value into 0.
 */
export function asPct(v) {
  if (!isNum(v)) return null;
  return v > 1 ? v : v * 100;
}

export function pct(v, dp = 0) {
  const p = asPct(v);
  return p == null ? UNKNOWN : `${p.toFixed(dp)}%`;
}

/** part/whole as a percentage, or `—` when either side is unknown. */
export function share(part, whole, dp = 0) {
  if (!isNum(part) || !isNum(whole) || whole === 0) return UNKNOWN;
  return `${((part / whole) * 100).toFixed(dp)}%`;
}

/**
 * Contract §3: a rate is meaningless without its denominator. "92%" over 25
 * ratings and "92%" over 25,000 are different claims and must not look alike.
 */
export function rateOf(rate, n, noun = 'rated') {
  const p = asPct(rate);
  if (p == null) return UNKNOWN;
  if (!isNum(n)) return `${p.toFixed(0)}% (sample size unknown)`;
  return `${p.toFixed(0)}% of ${int(n)} ${noun}`;
}

/**
 * Money. A cost with no configured price is NOT zero — §3 is explicit that a
 * zero reads as "free" and nobody notices for months. `null` renders as
 * "not configured" at the call site; this only formats a real number.
 */
export function usd(v, dp = 4) {
  if (!isNum(v)) return null;
  if (v === 0) return '$0.00';
  if (v < 0.01) return `$${v.toFixed(dp)}`;
  return `$${v.toFixed(2)}`;
}

/** Dollars per million tokens — the only unit in which two models compare. */
export function perMillion(costUsd, tokens) {
  if (!isNum(costUsd) || !isNum(tokens) || tokens === 0) return null;
  return `$${((costUsd / tokens) * 1e6).toFixed(2)}`;
}

/** "25.8:1" — a ratio, or null when either side is unknown. */
export function ratio(a, b, dp = 1) {
  if (!isNum(a) || !isNum(b) || b === 0) return null;
  return (a / b).toFixed(dp);
}

/**
 * The browser's IANA zone, sent as `tz` so buckets are cut at the reader's
 * midnight rather than at UTC's. Falls back to UTC rather than to nothing:
 * the server defaults to UTC anyway, and naming it keeps request and chip
 * telling the same story.
 */
export function browserZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

/** Signed formatters for a movement chip, one per unit on the page. */
export const signedInt = (v) => (v > 0 ? `+${int(v)}` : int(v));
export const signedPct = (v) => `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(1)}pp`;

/**
 * A points formatter that reads its SCALE off the delta block it belongs to.
 *
 * Rates cross the wire in two units: `cache_rate` and `burmese_share` are
 * fractions (0..1), while a percentage-shaped field may already be 0..100.
 * `asPct` disambiguates a VALUE with `v > 1`, but that heuristic is useless on a
 * delta — a movement of 0.06 is either six points or six hundredths of one, and
 * the number alone cannot say which. Formatting a fraction as points silently
 * renders a 6-point swing as "+0.1pp", which is not a rounding error but a
 * different claim.
 *
 * The block carries its own `value`, so the scale is read from that rather than
 * guessed from the movement. Falls back to treating the delta as already in
 * points when there is no value to learn from.
 */
export function signedPctOf(block) {
  const fractional = isNum(block?.value) ? Math.abs(block.value) <= 1 : false;
  return (v) => {
    const pts = fractional ? v * 100 : v;
    return `${pts > 0 ? '+' : pts < 0 ? '−' : ''}${Math.abs(pts).toFixed(1)}pp`;
  };
}
export const signedSecs = (v) => `${v > 0 ? '+' : '−'}${Math.abs(v / 1000).toFixed(1)}s`;
export const signedUsd = (v) => `${v > 0 ? '+' : '−'}$${Math.abs(v).toFixed(4)}`;

/** Clock time only — the per-call table is read within one day. */
export function clock(ts) {
  if (!ts) return UNKNOWN;
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function when(ts) {
  if (!ts) return UNKNOWN;
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  return d.toLocaleString(undefined, {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit'
  });
}

export function whenFull(ts) {
  if (!ts) return UNKNOWN;
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  return d.toLocaleString(undefined, {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  });
}

/** "11 Aug" from an ISO day or an hour bucket. */
export function dayLabel(t) {
  if (!t) return UNKNOWN;
  const iso = String(t).length === 10 ? `${t}T00:00:00` : String(t).replace(' ', 'T');
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(t);
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}

/** The ISO day part of a bucket key, for a drill-down that filters by day. */
export const isoDay = (t) => (t ? String(t).slice(0, 10) : '');

export const clip = (s, n = 64) => (!s ? '' : s.length > n ? s.slice(0, n - 1) + '…' : s);

/** Language code → something a human reads. Unknown codes pass through. */
export function langName(code) {
  if (!code || code === '?') return UNKNOWN;
  const map = { en: 'English', my: 'Burmese' };
  return map[code] ? `${code} (${map[code]})` : String(code);
}
