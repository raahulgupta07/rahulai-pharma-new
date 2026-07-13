# City Pharma Agent — embed integration (for the host app engineer)

Drop the chat widget on any page and it answers from the pharmacy data. With a
**signed `store_id`**, the agent only sees that store's stock/price — "data by store"
is enforced server-side; the user can't read another branch.

You need exactly two things before you paste anything:

1. **The public backend base URL** — the address a *customer's browser* can reach,
   e.g. `https://agent.example.com`. Not `localhost`, not an internal IP. Every URL
   below is written as `$BASE`; substitute your own.
2. **A registered embed credential** — an `(embed_id, public_key)` pair. Mint one in
   the admin console under **Tenants**, then read the ready-made snippet (already
   filled with your real values) under **Embed widget**.

---

## 0. Credentials are FAIL-CLOSED — read this first

`POST /api/embed/session/create` validates `(embed_id, public_key)` against the
registered credential store. An unregistered pair is rejected:

```
HTTP 403  {"detail": "invalid embed credentials"}
```

There is no open/anonymous mode. A missing or wrong credential does not degrade to
a public session — it fails.

**The `web` / `web` credential is DEVELOPMENT-ONLY.** It is seeded at boot when
`EMBED_DEV_CREDENTIAL=true` (the default on a dev backend) **and** no credential is
registered yet, so the widget works out of the box locally. Production sets
`EMBED_DEV_CREDENTIAL=false` and it is **not** seeded. If you copy a snippet carrying
`data-embed-id="web"` into a production page, every session create returns **403** and
the bubble never opens. Register a real credential and use that.

Register one (admin console → **Tenants**, or the admin API with a Bearer JWT):

```
POST $BASE/admin/credentials   { "embed_id": "citymart-web", "public_key": "pk_live_…" }
GET  $BASE/admin/credentials   -> [ { "embed_id": "...", "public_key": "..." } ]
```

The `public_key` is not a secret — it ships in the page source. It is a tenant
identifier, not an authenticator; the thing that actually protects store data is the
**HMAC signature** in section 2, whose `SECRET_KEY` never leaves your server.

---

## 1. One-line widget (no auth — all stores)

```html
<script src="https://agent.example.com/api/embed/widget.js"
  data-embed-id="citymart-web"
  data-public-key="pk_live_…"
  data-title="CityCare Agent"
  data-greeting="Ask about stock, prices, substitutes, or indications."
  data-accent="#006869"
  data-stream="true" async></script>
```

A floating chat bubble appears bottom-right. It calls `/api/embed/session/create`
then streams answers from `/api/embed/chat/stream`.

`data-accent` defaults to `#006869` (teal) if omitted — that is the current product
accent. (It used to be `#c96342`; if you have that hardcoded in a live page, it still
works, it's just off-brand.)

---

## 2. Store-scoped widget (data by store — recommended)

Sign the user **server-side** (never expose the secret to the browser), pass the
signed `store_id`. The backend verifies the signature and locks every answer to
that store.

### Laravel / PHP (matches your existing `CityAgentClient`)

```php
// config/services.php
'cityagent' => [
    'base_url'   => env('CITYAGENT_BASE_URL'),   // https://agent.example.com
    'secret_key' => env('CITYAGENT_SECRET_KEY'), // == backend SECRET_KEY (.env)
    'embed_id'   => env('CITYAGENT_EMBED_ID'),   // from admin → Tenants
    'public_key' => env('CITYAGENT_PUBLIC_KEY'),
],
```

```php
// Controller — sign the user with their store
$user = [
    'id'       => (string) $currentUser->id,
    'store_id' => (string) $currentUser->branch,   // e.g. "20060-CCBHSC"
];
// canonical = sorted keys, no spaces, unescaped — MUST byte-match the backend
ksort($user);
$canonical = json_encode($user, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
$signature = hash_hmac('sha256', $canonical, config('services.cityagent.secret_key'));
```

```blade
{{-- layout.blade.php --}}
<script src="{{ config('services.cityagent.base_url') }}/api/embed/widget.js"
  data-embed-id="{{ config('services.cityagent.embed_id') }}"
  data-public-key="{{ config('services.cityagent.public_key') }}"
  data-user='@json($user)'
  data-user-sig="{{ $signature }}"
  data-title="CityCare Agent"
  data-greeting="Ask about your branch's stock."
  data-accent="#006869"
  data-stream="true" async></script>
```

That's it — the widget sends `user` + `signature`; the backend mints a session
token carrying the verified `store_id`; all tool calls are forced to that store.

---

## 3. Raw API (if not using the widget)

```
POST $BASE/api/embed/session/create
  { "embed_id": "citymart-web", "public_key": "pk_live_…",
    "user": {"id":"42","store_id":"20060-CCBHSC"}, "signature": "<hmac>" }
  -> 200 { "session_token": "...", "expires_in": 900 }
  -> 403 { "detail": "invalid embed credentials" }   # pair not registered
  -> 401 { "detail": "bad user signature" }          # HMAC mismatch

POST $BASE/api/embed/chat          { "session_token": "...", "message": "..." } -> { "content": "..." }
POST $BASE/api/embed/chat/stream   -> SSE: data: {"delta":"..."}  ... data: [DONE]
```

Session tokens expire (`expires_in`, 900s default); re-mint on a 401 from `/chat*`.

Your existing `CityAgentClient.php` (session/chat/stream + HMAC) works unchanged —
set `CITYAGENT_BASE_URL` to this backend, `CITYAGENT_SECRET_KEY` to match, and the
embed_id/public_key to a **registered** credential.

---

## 4. HMAC must match (cross-language)

Both sides compute the signature over the SAME canonical bytes:

| | rule |
|---|---|
| keys | sorted alphabetically (`ksort`) |
| spacing | none (`{"id":"42","store_id":"X"}`) |
| escaping | slashes + unicode NOT escaped |
| algo | HMAC-SHA256(canonical, SECRET_KEY) hex |

Backend reference: `app/security.py::canonical`. Verified identical to PHP
`json_encode(..., JSON_UNESCAPED_SLASHES|JSON_UNESCAPED_UNICODE)` on sorted keys.

---

## 5. Test it (data by store)

1. Register a credential (Tenants) and note `embed_id` / `public_key`.
2. Set `CITYAGENT_SECRET_KEY` = backend `SECRET_KEY` (.env).
3. Sign a user with `store_id` = a real site code (e.g. `20060-CCBHSC`).
4. Drop the script tag, open the page, ask: **"top 5 by stock here"** or
   **"stock of article 1000000024029"** — answers are limited to that store.
5. Change `store_id` to another branch → answers change to that branch only.
6. A user signed for store A **cannot** get store B's numbers, even if they ask.

Quick curl test (replace SIG — sign `{"id":"42","store_id":"20060-CCBHSC"}`):
```
curl -X POST $BASE/api/embed/session/create -H 'Content-Type: application/json' \
  -d '{"embed_id":"citymart-web","public_key":"pk_live_…","user":{"id":"42","store_id":"20060-CCBHSC"},"signature":"<SIG>"}'
```
A `403 invalid embed credentials` here means the pair is not registered on *this*
backend — check Tenants on the environment you are pointing at, not the dev one.

---

## Common failure modes

| symptom | cause |
|---|---|
| bubble never opens, 403 on session/create | credential not registered on this backend (often: a dev `web`/`web` snippet pasted into prod) |
| works on staging, 403 in prod | credentials are per-backend; register on prod too |
| 401 on session/create | HMAC mismatch — canonical bytes or `SECRET_KEY` differ |
| widget loads nothing, browser console shows a blocked request | base URL is `localhost` / internal, not reachable from the customer's browser |
| answers show another branch's stock | you didn't sign a `store_id` — an unsigned session is unscoped (all stores) |

---

## What the engineer does NOT need to touch
- No embeddings/AI code — that's internal to the backend (semantic search runs
  server-side; the widget just sends text and gets an answer).
- No database access — the agent reads the pharmacy data for you.
- Just: a registered credential + script tag + server-side HMAC of `{id, store_id}`.
