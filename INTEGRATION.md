# City Pharma Agent — embed integration (for the host app engineer)

Drop the chat widget on any page and it answers from the pharmacy data. With a
**signed `store_id`**, the agent only sees that store's stock/price — "data by store"
is enforced server-side; the user can't read another branch.

Backend base URL (point everything here): `http://YOUR_BACKEND:8088`

---

## 1. One-line widget (no auth — all stores)

```html
<script src="http://YOUR_BACKEND:8088/api/embed/widget.js"
  data-embed-id="web"
  data-public-key="web"
  data-title="CityCare Agent"
  data-greeting="Ask about stock, prices, substitutes, or indications."
  data-accent="#c96342"
  data-stream="true" async></script>
```

A floating chat bubble appears bottom-right. It calls `/api/embed/session/create`
then streams answers from `/api/embed/chat/stream`.

---

## 2. Store-scoped widget (data by store — recommended)

Sign the user **server-side** (never expose the secret to the browser), pass the
signed `store_id`. The backend verifies the signature and locks every answer to
that store.

### Laravel / PHP (matches your existing `CityAgentClient`)

```php
// config/services.php
'cityagent' => [
    'base_url'   => env('CITYAGENT_BASE_URL'),   // http://YOUR_BACKEND:8088
    'secret_key' => env('CITYAGENT_SECRET_KEY'), // == backend SECRET_KEY (.env)
],
```

```php
// Controller — sign the user with their store
$user = [
    'id'       => (string) $currentUser->id,
    'store_id' => (string) $currentUser->branch,   // e.g. "20060-CCBHSC"
];
// canonical = sorted keys, no spaces, unescaped — MUST byte-match the backend
$canonical = json_encode($user, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
// (ksort first if keys aren't already alphabetical)
ksort($user);
$canonical = json_encode($user, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
$signature = hash_hmac('sha256', $canonical, config('services.cityagent.secret_key'));
```

```blade
{{-- layout.blade.php --}}
<script src="{{ config('services.cityagent.base_url') }}/api/embed/widget.js"
  data-embed-id="{{ $embedId }}"
  data-public-key="{{ $publicKey }}"
  data-user='@json($user)'
  data-user-sig="{{ $signature }}"
  data-title="CityCare Agent"
  data-greeting="Ask about your branch's stock."
  data-accent="#c96342"
  data-stream="true" async></script>
```

That's it — the widget sends `user` + `signature`; the backend mints a session
token carrying the verified `store_id`; all tool calls are forced to that store.

---

## 3. Raw API (if not using the widget)

```
POST /api/embed/session/create
  { "embed_id": "...", "public_key": "...",
    "user": {"id":"42","store_id":"20060-CCBHSC"}, "signature": "<hmac>" }
  -> { "session_token": "...", "expires_in": 900 }

POST /api/embed/chat          { "session_token": "...", "message": "..." } -> { "content": "..." }
POST /api/embed/chat/stream   -> SSE: data: {"delta":"..."}  ... data: [DONE]
```

Your existing `CityAgentClient.php` (session/chat/stream + HMAC) works unchanged —
just set `CITYAGENT_BASE_URL` to this backend and `CITYAGENT_SECRET_KEY` to match.

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

1. Set `CITYAGENT_SECRET_KEY` = backend `SECRET_KEY` (.env).
2. Sign a user with `store_id` = a real site code (e.g. `20060-CCBHSC`).
3. Drop the script tag, open the page, ask: **"top 5 by stock here"** or
   **"stock of article 1000000024029"** — answers are limited to that store.
4. Change `store_id` to another branch → answers change to that branch only.
5. A user signed for store A **cannot** get store B's numbers, even if they ask.

Quick curl test (replace SIG — sign `{"id":"42","store_id":"20060-CCBHSC"}`):
```
curl -X POST $BASE/api/embed/session/create -H 'Content-Type: application/json' \
  -d '{"embed_id":"web","public_key":"web","user":{"id":"42","store_id":"20060-CCBHSC"},"signature":"<SIG>"}'
```

---

## What the engineer does NOT need to touch
- No embeddings/AI code — that's internal to the backend (semantic search runs
  server-side; the widget just sends text and gets an answer).
- No database access — the agent reads the pharmacy data for you.
- Just: script tag + server-side HMAC of `{id, store_id}`.
