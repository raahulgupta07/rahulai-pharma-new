# Purging pytest debris from the live database — 2026-08-17

**Nothing here has been executed.** Deleting live rows is the owner's call. Every
count below was measured against the running `:8091` database on 2026-08-17.

## What happened

Until today the test suite ran against the live Postgres and the live Redis. It
created users, hammered the login endpoint, uploaded branding, and wrote SSH
keys — all into the database the console reads. `tests/dbguard.py` now redirects
the suite to its own database and refuses to start without one, so **this is a
one-off cleanup of history, not a recurring chore.**

The debris is not cosmetic. On the Activity page the generated accounts
out-ranked the only real administrator 4:1, and `admin.sftp.keys.create` was the
single most frequent action in the product — 28 times in one day, all of it a
test fixture.

## The predicate: use `ip`, not the email pattern

Two ways to identify the rows, measured:

| predicate | `app_events` matched |
|---|---|
| `actor_email ~ '^(appr\|brand\|sftp\|sec\|scope\|authz\|emb\|test\|pytest\|jit\|cors\|leak\|fab)-[0-9a-f]{6,}@'` | 116 |
| `ip = 'testclient'` | **144** |
| either | 144 |

`ip = 'testclient'` is strictly better and simpler. `testclient` is the address
FastAPI's own `TestClient` reports — a real browser can never present it — and
it catches 28 further rows the email pattern misses, because some test requests
carry no actor at all. The hand-written prefix list is the weaker instrument:
it is a guess about fixture naming that goes stale the moment someone adds a
fixture with a new prefix.

**Neither predicate touches the real administrator.** Verified, not assumed:

```sql
-- must return 0
SELECT count(*) FROM app_events
 WHERE ip = 'testclient' AND actor_email = 'admin@citcare.local';
```

Measured: `0`. The real admin's 5 events are all from `192.168.65.1`.

## Current state

| table | total | debris | keep |
|---|---:|---:|---:|
| `app_events` | 149 | 144 | 5 |
| `users` | 3 | 2 | 1 |
| `auth_events` | 57 | 20 | 37 |
| `chat_logs` | 136 | see below | — |

## 1. `app_events` — 144 of 149

```sql
-- LOOK FIRST
SELECT ts, actor_email, action, ip FROM app_events
 WHERE ip = 'testclient' ORDER BY ts DESC LIMIT 20;

-- THEN, if you accept it
DELETE FROM app_events WHERE ip = 'testclient';   -- 144 rows
```

## 2. `users` — 2 of 3

```sql
SELECT id, email, role, created_at FROM users
 WHERE email ~ '^(appr|brand|sftp|sec|scope|authz|emb|test|pytest|jit|cors|leak|fab)-[0-9a-f]{6,}@';
```

Returns exactly:

| id | email | role | created |
|---|---|---|---|
| 6 | `appr-188413e477@corp.mm` | admin | 2026-07-10 |
| 9 | `appr-150e95de29@corp.mm` | admin | 2026-07-10 |

Both are `admin` role and `approved` — if LDAP or SSO is ever enabled they are
real accounts an attacker could authenticate as, which is the one reason to
lean toward deleting rather than leaving them.

```sql
DELETE FROM users WHERE id IN (6, 9);   -- by primary key, not by pattern
```

Deleting by the two ids rather than re-running the regex means the statement
cannot widen if the pattern is wrong.

## 3. `auth_events` — 20 of 57

```sql
SELECT event, email, ip, ts FROM auth_events WHERE ip = 'testclient' ORDER BY ts DESC;
DELETE FROM auth_events WHERE ip = 'testclient';   -- 20 rows
```

This is where the 12 lock-outs and most of the failed sign-ins come from — the
suite testing the lockout guard against the live database. The 37 rows from
`192.168.65.1` are real sign-ins and must stay.

## 4. `chat_logs` — recommend NOT purging

136 turns, of which 122 predate the instrumentation and carry no path, model or
cost. Some were certainly produced by tests, but there is **no reliable marker**
— `chat_logs` records no ip and no actor for that era, which is the same gap the
Users & sessions tab is blocked on. Guessing from question text would delete
real pharmacist questions.

Leave them. They are already rendered honestly as `not recorded` rather than
being folded into a bucket, and 122 unknown turns are less harmful than an
unknown number of deleted real ones.

## Before running anything

```bash
docker exec pharmacy-opt-postgres-1 pg_dump -U <user> -d <db> \
  -t app_events -t auth_events -t users \
  > _backups/pre-purge-20260817.sql
```

Three `DELETE`s, no `WHERE` typos, one backup. If the backup is not written,
do not run the deletes — `app_events` has no other copy.

## After

The Activity page's "Where it came from" panel should collapse to a single
`192.168.65.1` row. That panel is the fastest way to confirm the purge landed and
that the test isolation is holding: if `testclient` reappears, something is still
pointed at the live database.
