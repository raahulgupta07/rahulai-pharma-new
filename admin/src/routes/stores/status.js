/**
 * Deriving a branch's state from a `/admin/stores` registry row.
 *
 * The registry (1.7.0) separates two facts that used to be one. A branch
 * EXISTS because it has a row in `stores`; a branch has STOCK because it has
 * rows in `inventory`, which is truncate-and-reload from the daily file. So a
 * branch can now legitimately exist with no stock at all, and the console has
 * to say which of the two is true rather than showing an empty line.
 *
 * Four states, and every one of them carries its own word AND its own glyph on
 * screen — never a colour on its own. Order matters and is not arbitrary:
 *
 *   1. DISABLED  an admin hid it. This outranks everything else, because it is
 *                the only state a person chose, and a disabled branch with no
 *                stock must still read as "we hid this", not "the file is
 *                broken".
 *   2. NODATA    no inventory rows, or it is missing from the latest file.
 *   3. NEW       first seen within the last NEW_DAYS days.
 *   4. ACTIVE    everything else.
 *
 * `first_seen` being NULL does NOT make a branch new. Right now every row the
 * backend returns has null timestamps (the registry seeds at boot and this
 * console can be pointed at a backend that has not restarted yet), and a
 * missing date must never be read as "today" — that would flag all 53 branches
 * as new on the day someone deploys.
 */

export const DISABLED = 'disabled';
export const NODATA = 'nodata';
export const NEW = 'new';
export const ACTIVE = 'active';

/** A branch counts as new for this long after `first_seen`. */
export const NEW_DAYS = 7;

const DAY_MS = 86_400_000;

/** Epoch ms from an ISO timestamp, or null. Never NaN, never 0-for-missing. */
export function at(v) {
  if (!v) return null;
  const t = Date.parse(v);
  return Number.isFinite(t) ? t : null;
}

/**
 * Does this branch have inventory behind it?
 *
 * `skus` is a row count, so `> 0` is the honest test. A null (a backend that
 * did not send the column) is UNKNOWN and is treated as "no data" rather than
 * silently rendered as a zero — same rule the rest of the console runs on.
 */
export function hasStock(row) {
  const n = row?.skus;
  return typeof n === 'number' && Number.isFinite(n) && n > 0;
}

/** True when the branch was absent from the most recent inventory file. */
export function missingFromFile(row) {
  return at(row?.missing_since) !== null;
}

/** One of DISABLED / NODATA / NEW / ACTIVE. */
export function derive(row, now = Date.now()) {
  if (row?.status === 'disabled') return DISABLED;
  if (!hasStock(row) || missingFromFile(row)) return NODATA;
  const seen = at(row?.first_seen);
  if (seen !== null && now - seen <= NEW_DAYS * DAY_MS) return NEW;
  return ACTIVE;
}

/** The word on screen. Also what a screen reader announces. */
export const STATE_LABEL = {
  [ACTIVE]: 'Active',
  [NEW]: 'New',
  [NODATA]: 'No data',
  [DISABLED]: 'Disabled'
};

/** Tally of every state across a list, for the count line under the title. */
export function tally(rows, now = Date.now()) {
  const out = { [ACTIVE]: 0, [NEW]: 0, [NODATA]: 0, [DISABLED]: 0 };
  for (const r of rows) out[derive(r, now)] += 1;
  return out;
}

/**
 * The filter chips. `id` is what the chip stores; `test` decides membership.
 *
 * "Not in latest file" is deliberately NOT the same question as "No data": a
 * branch can have no stock rows while still appearing in the file, and a
 * broken export is the case an operator actually wants to isolate.
 */
export const FILTERS = [
  { id: 'all', label: 'All', test: () => true },
  { id: ACTIVE, label: 'Active', test: (r, now) => derive(r, now) === ACTIVE },
  { id: DISABLED, label: 'Disabled', test: (r) => r?.status === 'disabled' },
  { id: 'missing', label: 'Not in latest file', test: (r) => missingFromFile(r) }
];
