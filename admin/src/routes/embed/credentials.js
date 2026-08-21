// Which credential the snippets on this page are signed with — ONE rule, in one
// place.
//
// The rule lived twice, in two different shapes. GuidePanel skipped the dev pair
// and WidgetPanel took `creds[0]`, which may BE the dev pair. A snippet signed
// `web`/`web` validates on a dev stack and then 401s on the customer's website,
// so the two panels disagreeing was never a tidiness problem: one of them handed
// an operator a snippet to paste onto a real site, where it silently does not
// answer.
//
// This mirrors the backend's `_default_embed_credential` (app/admin.py), which
// resolves the same question for `GET /admin/stores/{code}/embed`. That endpoint
// is deliberately NOT called from here, for two reasons: it is
// `require_super_admin` while these panels are open to `admin`, so an operator
// who can use this page today would get a 403 instead of a snippet; and it
// resolves the credential for ONE BRANCH, while both panels also emit a
// store-less public snippet that no branch code belongs to. The duplication
// across the process boundary is therefore intentional — but it is one copy on
// this side, and it is written to the same rule. Change one, change the other.

/** The auto-seeded development pair (`ensure_dev_credential`, app/cache.py). */
export const DEV_CREDENTIAL = { embed_id: 'web', public_key: 'web' };

/**
 * What a snippet carries when there is nothing real to put in it.
 *
 * Shouted, never a plausible-looking `web`/`web`: the backend's credential check
 * is fail-closed, so a placeholder is rejected loudly at the customer's first
 * request instead of appearing to work.
 */
export const PLACEHOLDER = { embed_id: 'YOUR_EMBED_ID', public_key: 'YOUR_PUBLIC_KEY' };

/** Is this the development-only pair? Both halves must match. */
export function isDevCredential(c) {
  return (
    !!c &&
    c.embed_id === DEV_CREDENTIAL.embed_id &&
    c.public_key === DEV_CREDENTIAL.public_key
  );
}

/**
 * The credential a page may DEFAULT to, or `null` when there is none.
 *
 * The dev pair is never a default anywhere. It is not seeded in production, so a
 * snippet carrying it is rejected there with `403 invalid embed credentials` —
 * and the operator copying that snippet has no way to see that from this
 * console, because it validates perfectly well against the stack they are
 * looking at.
 *
 * Ties are broken by sorting on `embed_id` rather than by list order, which is
 * Redis hash order and not stable in any sense the operator can see. Two loads
 * of this page must not default to two different tenants; a customer handed two
 * different snippets for one site cannot tell which is the working one.
 */
export function defaultCredential(creds) {
  const real = (Array.isArray(creds) ? creds : [])
    .filter((c) => c?.embed_id && c?.public_key && !isDevCredential(c))
    .sort((a, b) => String(a.embed_id).localeCompare(String(b.embed_id)));
  return real[0] ?? null;
}

/**
 * True when the dev pair is registered and is the ONLY thing that could be
 * defaulted to — the case a page must say out loud rather than paper over. It is
 * deliberately not "the list is empty": an empty list is a different sentence
 * (nothing is registered) with a different fix.
 */
export function onlyDevCredential(creds) {
  const list = Array.isArray(creds) ? creds : [];
  return list.some(isDevCredential) && defaultCredential(list) === null;
}
