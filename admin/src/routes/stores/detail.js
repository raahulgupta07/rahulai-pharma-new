/**
 * Helpers for the branch detail panel.
 *
 * `status.js` answers "what state is this branch in" from a REGISTRY row and is
 * deliberately untouched by this file — its rules are load-bearing for the
 * table, the filter chips and the count line. This file answers the separate
 * question "how do we render one branch's detail payload", and every helper in
 * it follows the console's honesty rule: `—` means we do not know, `0` means we
 * measured zero. Nothing here coerces a missing value into a number.
 */

import { UNKNOWN, isNum, int } from '$lib/charts/format.js';

/** Where the per-branch snippet is minted. One constant, so repointing is one line. */
export const EMBED_PATH = (code) => `/admin/stores/${encodeURIComponent(code)}/embed`;

/** Where the panel's payload comes from. Same reason. */
export const DETAIL_PATH = (code) => `/admin/stores/${encodeURIComponent(code)}/detail`;

/**
 * A money figure in kyat, or `—`.
 *
 * Not `usd()` from format.js: that one prefixes a dollar sign, and every stock
 * figure in this product is MMK. The currency is part of the number's meaning,
 * so it is never rendered bare.
 */
export function mmk(v) {
  return isNum(v) ? `${int(v)} MMK` : UNKNOWN;
}

/**
 * A share of the estate, already expressed in percent by the backend.
 *
 * Deliberately NOT `pct()` from format.js. That helper disambiguates a fraction
 * from a percentage with `v > 1 ? v : v * 100`, which is right for a rate that
 * crosses the wire in either unit — and wrong here. `pct_of_value` is always a
 * percentage, so a branch holding half a percent of the estate arrives as 0.5
 * and `pct()` would render it as "50%": a hundredfold overstatement of how much
 * of the company's stock is about to disappear from customer answers.
 */
export function estateShare(v) {
  return isNum(v) ? `${v.toFixed(1)}%` : UNKNOWN;
}

/**
 * A branch's cumulative model spend, to four decimal places.
 *
 * Deliberately not `usd()` from format.js, and this is the one place in the
 * panel that forks a shared formatter. `usd()` renders anything at or above a
 * cent to TWO places — `usd(0.32204)` is `"$0.32"` — which is right for money a
 * human hands over and wrong for this figure. A branch's whole conversational
 * spend sits in the third of a dollar for months, so at 2dp the number is
 * frozen: a week of traffic moves it by less than the rounding. The approved
 * design shows `$0.3220` for exactly that reason.
 *
 * What it does NOT fork is the honesty rule: an unpriced figure is `null` here
 * as it is there, so the caller still has to decide between "not configured"
 * and a measured zero rather than being handed a `$0.00`.
 */
export function agentCost(v) {
  return isNum(v) ? `$${v.toFixed(4)}` : null;
}

/**
 * Three states for an audit entry, from the status the backend recorded.
 *
 * This mirrors `$lib/ErrorState.svelte` rather than inventing a second scheme,
 * and for the same reason the analytics contract splits `tool_calls` three
 * ways: **a refusal is not a failure.** A 403 is a fact about who the actor
 * was; a 400 is a fact about what they sent. Neither says the system broke, and
 * painting them the same red as a 5xx is a claim the reader then has to
 * un-learn. Only a 5xx is a failure.
 *
 * A row with no status at all is UNKNOWN — never quietly filed as a success.
 */
export const OK = 'ok';
export const REFUSED = 'refused';
export const FAILED = 'failed';
export const UNCLEAR = 'unclear';

export function eventTone(status) {
  const s = Number(status);
  if (!Number.isFinite(s) || s <= 0) return UNCLEAR;
  if (s >= 500) return FAILED;
  if (s >= 400) return REFUSED;
  if (s >= 200 && s < 300) return OK;
  return UNCLEAR;
}

/**
 * The word a screen reader hears next to the numeric code.
 *
 * The code alone carries the distinction for a sighted reader — "403" and "200"
 * do not look alike whatever colour they are — but "403" read aloud is not an
 * outcome. The word is what makes the tint redundant rather than load-bearing.
 */
export const TONE_WORD = {
  [OK]: 'Done',
  [REFUSED]: 'Refused',
  [FAILED]: 'Failed',
  [UNCLEAR]: 'Outcome not recorded'
};

/**
 * Is this audit entry a raw system record rather than a written sentence?
 *
 * The backend writes a human sentence per known action ("hid it — 'branch
 * closed'"), and falls back to the route slug plus its status for an action
 * nobody has written copy for. That fallback is correct — it invents nothing —
 * but rendered in the same voice as the sentences it sits among, it reads as a
 * defect in our product rather than as a machine record with no description.
 *
 * The test is `summary` starting with `action`, which is exactly the shape of
 * that fallback and is a shape a written sentence never has. It fails SAFE in
 * both directions: if the backend changes the fallback format this returns
 * false and the row renders as it does today, and it can never suppress or
 * alter a real sentence — the summary is still shown verbatim either way, only
 * its styling differs.
 */
export function isSystemRecord(event) {
  const summary = (event?.summary ?? '').trim();
  const action = (event?.action ?? '').trim();
  return summary !== '' && action !== '' && summary.startsWith(action);
}

/** True when this branch has at least one attributable conversation. */
export function hasChats(d) {
  return isNum(d?.chats?.count) && d.chats.count > 0;
}

/**
 * True when we cannot date this branch.
 *
 * `first_seen` is NULL for every branch that predates the registry, and that is
 * correct rather than missing data. The panel says "Before tracking" and
 * explains why; it must never render today's date, which would claim all 53
 * branches opened on the day someone deployed.
 */
export function undated(d) {
  return !d?.first_seen;
}
