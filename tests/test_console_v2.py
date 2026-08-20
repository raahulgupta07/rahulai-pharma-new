"""Console v2: timezone buckets, period deltas, and the six new endpoints.

Covers the 2026-08-17 addendum to `docs/ANALYTICS_CONTRACT.md` — §A timezone,
§B period deltas, §C `/admin/activity/{summary,trends,explore,audit}` and
`/admin/analytics/{llm-calls,economics}`.

Five things are load-bearing here and each has a test that FAILS if it
regresses, rather than one that merely passes today:

1. **A day is the CALLER's day.** The database runs `Etc/UTC`; the console
   labels buckets in the browser's zone. A turn at 01:00 Asia/Yangon is 18:30
   UTC the day BEFORE, so before the fix six and a half hours of every Yangon
   morning were charted on the previous day. The test seeds exactly that turn
   and asserts the bucket MOVES when `tz` changes — an assertion a silent UTC
   fallback cannot satisfy.

2. **An unknown zone is a 400.** Never a fallback to UTC: a silently wrong
   bucket is the bug being fixed, and a fallback reinstates it while looking
   like it works.

3. **The pivot never interpolates a caller's string.** `measure` and `by` are
   whitelisted; an unknown one is refused rather than pasted into SQL.

4. **No prior window means `null`, not `0`.** `0%` reads as "no change", which
   is a claim, and the whole point of §B is not to make claims we cannot support.

5. **A scoped caller cannot widen scope with `store=`.** The scope is an
   enforced boundary read from the caller's own users row; `store` is the
   operator's filter box. They are ANDed.

Every filter test compares against the SAME endpoint unfiltered and asserts a
number MOVED. A test asserting 200-with-rows passes on every filter bug found in
`app/admin.py` so far, which is three.

Authorisation is exercised from the BOTTOM of the role ladder — a `viewer`, then
a plain `admin` — never as a super_admin. An admin-only authz test cannot see a
scope leak, because the super_admin scope is "everything".

⚠️ Like the rest of the suite this needs live Postgres, and every setup/teardown
goes through :func:`_pg`, a throwaway asyncpg connection — NOT ``app.db.q``,
whose pool is bound to the TestClient's portal loop.

⚠️ The seeded rows sit in a per-process window in the year 2099. Future, because
`prune_chat_logs` runs from the app lifespan that `api_client` starts and would
delete a window in the past mid-fixture; per-process, because two runs of this
file at once would otherwise double every count and fail as "the filter is
broken", which is the wrong thing to go and debug.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date, timedelta
from typing import Dict

import pytest

from app import auth as authmod
from app.config import get_settings

from tests.pgconn import pg

# Imported at collection time: app.api pulls in agno, which builds an
# asyncio.Lock() at import (see test_approval.py).
from app.api import app as _app  # noqa: F401

_RUN_DAY = date(2099, 6, 1) + timedelta(
    days=(uuid.uuid4().int + os.getpid()) % 3000
)
# D[0..5]. The CURRENT window is D0..D2 inclusive (bare dates include the whole
# day, §4), and the PREVIOUS window is therefore D-3..D-1 — which is why the
# fixture seeds into D-1 as well as into the current days.
_D = [(_RUN_DAY + timedelta(days=i)).isoformat() for i in range(-3, 6)]


def _d(i: int) -> str:
    """Day i, where 0 is the first day of the current window (negatives allowed)."""

    return _D[i + 3]


WINDOW_START = _d(0)
WINDOW_END = _d(2)
PREV_START = _d(-3)

MINE = "20005-CCYK"
SIBLING = "20024-CC73"

YANGON = "Asia/Yangon"        # UTC+06:30, and it has no DST to muddy the test


def _pg(query: str, *args, fetch: bool = False):
    """Run one statement on a private connection. Never touches app.db's pool.

    One connection per PROCESS, not per statement — see tests/pgconn.py for why
    the previous arrangement was the suite's whole wall clock. This file was the
    worst of it: the seeding fixture makes about thirty calls per test across
    forty-nine tests.
    """

    return pg(query, *args, fetch=fetch)


_SCHEMA_READY = False


def _ensure_schema():
    """The §1 schema plus the three event tables, so these tests do not depend on
    a migration or on an admin action having happened first.

    Idempotent, and statement-for-statement what `app/activity.py`,
    `app/auth.py` and `app/ingest_events.py` create at boot. Drift between them
    and this IS the bug — the endpoints are written against these columns.

    Run ONCE per process. It was running per test, which is seven `CREATE TABLE
    IF NOT EXISTS` round trips forty-nine times over to create nothing.
    """

    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    _SCHEMA_READY = True

    _pg(
        """CREATE TABLE IF NOT EXISTS tool_calls (
               id            bigserial PRIMARY KEY,
               turn_id       bigint      NOT NULL
                             REFERENCES chat_logs(id) ON DELETE CASCADE,
               seq           int         NOT NULL,
               name          text        NOT NULL,
               arguments     jsonb,
               outcome       text        NOT NULL
                             CHECK (outcome IN ('succeeded','refused','failed')),
               error_message text,
               attempt       int         NOT NULL DEFAULT 1,
               duration_ms   int,
               ts            timestamptz NOT NULL DEFAULT now()
           )"""
    )
    _pg(
        """CREATE TABLE IF NOT EXISTS llm_calls (
               id                    bigserial PRIMARY KEY,
               turn_id               bigint      NOT NULL
                                     REFERENCES chat_logs(id) ON DELETE CASCADE,
               seq                   int         NOT NULL,
               model                 text,
               prompt_tokens         int,
               completion_tokens     int,
               reasoning_tokens      int,
               cache_read_tokens     int,
               cache_creation_tokens int,
               ttft_ms               int,
               duration_ms           int,
               cost_usd              numeric(12,6),
               cost_is_estimated     boolean     NOT NULL DEFAULT false,
               finish_reason         text,
               ts                    timestamptz NOT NULL DEFAULT now()
           )"""
    )
    for col, typ in (("actor_email", "text"), ("actor_role", "text"),
                     ("gave_up", "boolean")):
        _pg(f"ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS {col} {typ}")
    _pg("ALTER TABLE users ADD COLUMN IF NOT EXISTS store_id TEXT")
    _pg(
        """CREATE TABLE IF NOT EXISTS app_events (
               id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(),
               actor_email TEXT, actor_role TEXT, action TEXT NOT NULL,
               target TEXT, method TEXT, path TEXT, status INT, detail JSONB,
               ip TEXT, duration_ms INT
           )"""
    )
    _pg(
        """CREATE TABLE IF NOT EXISTS auth_events (
               id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(),
               event TEXT NOT NULL, email TEXT, actor_email TEXT, ip TEXT,
               detail TEXT
           )"""
    )
    _pg(
        """CREATE TABLE IF NOT EXISTS ingest_events (
               id BIGSERIAL PRIMARY KEY, run_id UUID NOT NULL, file TEXT NOT NULL,
               stamped TEXT, kind TEXT, step TEXT NOT NULL, status TEXT NOT NULL,
               detail TEXT, data JSONB, at TIMESTAMPTZ NOT NULL DEFAULT now()
           )"""
    )


# ---- seeding ---------------------------------------------------------------


def _turn(*, ts, store=MINE, question="q", answer="a", lang="EN", path="agent",
          model="test/turn-model", cached=False, latency=1000, tokens=100,
          cost="0.010000") -> int:
    """One turn.

    The turn-level token/cost columns are populated deliberately:
    `/analytics/cost` computes `available` from "does any matching row carry a
    non-NULL token count", so a turn seeded without them makes that endpoint
    answer "we have not measured this" — correctly — and a test written against
    it would then be asserting nothing.
    """

    rows = _pg(
        """INSERT INTO chat_logs
               (ts, store_id, question, answer, lang, path, model, cached,
                latency_ms, input_tokens, output_tokens, total_tokens, cost_usd)
           VALUES ($1::text::timestamptz,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,
                   $13::text::numeric)
           RETURNING id""",
        ts, store, question, answer, lang, path, model, cached, latency,
        tokens, tokens, tokens * 2, cost,
        fetch=True,
    )
    return rows[0]["id"]


def _llm(turn_id, seq, model, *, prompt=None, completion=None, cache_read=None,
         cost=None, estimated=False, ttft=None):
    _pg(
        """INSERT INTO llm_calls (turn_id, seq, model, prompt_tokens,
                                  completion_tokens, cache_read_tokens,
                                  cost_usd, cost_is_estimated, ttft_ms)
           VALUES ($1,$2,$3,$4,$5,$6,$7::text::numeric,$8,$9)""",
        turn_id, seq, model, prompt, completion, cache_read,
        None if cost is None else str(cost), estimated, ttft,
    )


def _app_event(*, ts, action, actor=None, status=None, ip=None, target=None,
               duration=None):
    _pg(
        """INSERT INTO app_events (ts, actor_email, actor_role, action, target,
                                   method, path, status, ip, duration_ms, detail)
           VALUES ($1::text::timestamptz,$2,'admin',$3,$4,'GET','/x',$5,$6,$7,
                   '{}'::jsonb)""",
        ts, actor, action, target, status, ip, duration,
    )


def _auth_event(*, ts, event, email, ip="9.9.9.9"):
    _pg(
        """INSERT INTO auth_events (ts, event, email, ip, detail)
           VALUES ($1::text::timestamptz,$2,$3,$4,'seeded')""",
        ts, event, email, ip,
    )


def _ingest_event(*, at, file, step, status="bad"):
    _pg(
        """INSERT INTO ingest_events (run_id, file, step, status, at)
           VALUES (gen_random_uuid(),$1,$2,$3,$4::text::timestamptz)""",
        file, step, status, at,
    )


def _purge():
    """Delete anything already sitting in THIS process's window, current AND prior.

    Teardown deletes by id, which a killed run never reaches, and a stranded row
    makes the NEXT run fail on a count assertion somewhere unrelated — where it
    reads as a broken filter rather than as debris. Purging on the way IN means a
    run inherits a clean slate instead of trusting the previous one to have
    exited politely. The window is per-process, so this cannot disturb a
    concurrent run.
    """

    lo, hi = f"{_d(-3)} 00:00:00+00", f"{_d(5)} 00:00:00+00"
    for table, col in (("chat_logs", "ts"), ("app_events", "ts"),
                       ("auth_events", "ts"), ("ingest_events", "at")):
        _pg(
            f"DELETE FROM {table} WHERE {col} >= $1::text::timestamptz"
            f"                      AND {col} <  $2::text::timestamptz",
            lo, hi,
        )


@pytest.fixture
def seeded():
    """Turns, model calls and events across the current AND previous windows.

    The previous half is not decoration: it is the only thing that can tell a
    working delta apart from one that always reports "no prior period", and the
    only thing that makes `movers` mean anything.
    """

    _ensure_schema()
    _purge()
    tag = uuid.uuid4().hex[:8]

    # -- the timezone probe ---------------------------------------------------
    # 18:30 UTC on D0 is 01:00 on D1 in Asia/Yangon. In UTC it buckets on D0; in
    # Yangon it buckets on D1. Nothing else in the window sits on D1, so the two
    # answers are distinguishable rather than merely different-looking.
    tz_turn = _turn(ts=f"{_d(0)} 18:30:00+00", question=f"yangon-dawn {tag}")

    # -- turns with model calls ----------------------------------------------
    a = _turn(ts=f"{_d(0)} 04:00:00+00", question=f"stock {tag}")
    _llm(a, 0, "test/priced", prompt=1000, completion=40, cache_read=250,
         cost="0.100000", ttft=300)
    # An UNPRICED call: cost must stay null downstream, never 0.0.
    _llm(a, 1, "test/unpriced", prompt=500, completion=10, cache_read=0,
         cost=None, ttft=800)

    b = _turn(ts=f"{_d(1)} 04:00:00+00", store=SIBLING, lang="MY",
              question=f"price {tag}")
    _llm(b, 0, "test/priced", prompt=2000, completion=60, cache_read=1000,
         cost="0.200000", ttft=400)

    # -- app_events: two clients, one 403 ------------------------------------
    _app_event(ts=f"{_d(0)} 03:00:00+00", action=f"settings.update.{tag}",
               actor=f"one-{tag}@corp.mm", status=200, ip="1.2.3.4", duration=12)
    _app_event(ts=f"{_d(0)} 03:30:00+00", action=f"settings.update.{tag}",
               actor=f"one-{tag}@corp.mm", status=200, ip="1.2.3.4", duration=30)
    _app_event(ts=f"{_d(1)} 03:00:00+00", action=f"users.delete.{tag}",
               actor=f"two-{tag}@corp.mm", status=403, ip="1.2.3.4", duration=4)
    # The suite's own fingerprint. A traffic number that cannot separate this
    # from a real request is measuring itself (see tests/conftest.py).
    _app_event(ts=f"{_d(1)} 03:10:00+00", action=f"settings.update.{tag}",
               actor=f"bot-{tag}@corp.mm", status=200, ip="testclient")

    # -- auth_events ----------------------------------------------------------
    # One with NO address, so the "not recorded" client band is exercised rather
    # than assumed: an event whose ip was never captured is neither a browser
    # nor the test suite, and folding it into either is a guess wearing a number.
    _auth_event(ts=f"{_d(0)} 02:00:00+00", event="login_ok",
                email=f"one-{tag}@corp.mm", ip=None)
    _auth_event(ts=f"{_d(1)} 02:00:00+00", event="login_fail",
                email=f"two-{tag}@corp.mm")
    _auth_event(ts=f"{_d(1)} 02:05:00+00", event="login_fail",
                email=f"two-{tag}@corp.mm")

    # -- the pipeline ---------------------------------------------------------
    _ingest_event(at=f"{_d(1)} 05:00:00+00", file=f"bad-{tag}.xlsx",
                  step="set_aside")

    # -- the PREVIOUS window (D-3..D-1) --------------------------------------
    _app_event(ts=f"{_d(-1)} 03:00:00+00", action=f"settings.update.{tag}",
               actor=f"one-{tag}@corp.mm", status=200, ip="1.2.3.4")
    _auth_event(ts=f"{_d(-1)} 02:00:00+00", event="login_ok",
                email=f"one-{tag}@corp.mm")

    ids = {"tag": tag, "tz_turn": tz_turn, "a": a, "b": b}
    yield ids
    _pg("DELETE FROM chat_logs WHERE id = ANY($1::bigint[])", [tz_turn, a, b])
    _purge()


# ---- callers ----------------------------------------------------------------


class _User:
    """An approved account + bearer header, optionally pinned to a branch."""

    def __init__(self, role="admin", store_id=None):
        _ensure_schema()
        self.email = f"cv2-{uuid.uuid4().hex[:10]}@corp.mm"
        rows = _pg(
            """INSERT INTO users (email, name, role, auth_sources, active,
                                  approved, store_id)
               VALUES ($1,'Console v2',$2,ARRAY['local'],TRUE,TRUE,$3)
               RETURNING id, email, role""",
            self.email, role, store_id, fetch=True,
        )
        self.id = rows[0]["id"]
        self.token = authmod.make_token(rows[0])["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def drop(self):
        _pg("DELETE FROM users WHERE id=$1", self.id)


@pytest.fixture
def admin():
    """The LEAST-privileged caller that can reach /admin/* — a plain admin."""

    u = _User()
    yield u
    u.drop()


@pytest.fixture
def pinned():
    """A branch manager: role `admin`, pinned to MINE. Nothing weaker exists."""

    u = _User(store_id=MINE)
    yield u
    u.drop()


@pytest.fixture
def root():
    """super_admin — the only role the activity console admits."""

    u = _User(role="super_admin")
    yield u
    u.drop()


def _get(client, path, headers, expect=200, **params):
    """GET with the seeded window applied by default, so real rows stay out."""

    p = {"start": WINDOW_START, "end": WINDOW_END}
    p.update(params)
    p = {k: v for k, v in p.items() if v is not None}
    r = client.get(path, params=p, headers=headers)
    assert r.status_code == expect, r.text
    return r.json()


ACTIVITY_PATHS = ["/admin/activity/summary", "/admin/activity/trends",
                  "/admin/activity/explore", "/admin/activity/audit"]
ANALYTICS_PATHS = ["/admin/analytics/llm-calls", "/admin/analytics/economics"]
ALL_NEW = ACTIVITY_PATHS + ANALYTICS_PATHS


# ---- 0. authorisation, from the bottom of the ladder ------------------------


@pytest.mark.parametrize("path", ALL_NEW)
def test_new_endpoints_reject_an_unauthenticated_caller(api_client, path):
    r = api_client.get(path)
    assert r.status_code in (401, 403)


@pytest.mark.parametrize("path", ALL_NEW)
def test_new_endpoints_reject_a_viewer(api_client, path):
    """A `viewer` authenticates fine and must still be refused everywhere."""

    u = _User(role="viewer")
    try:
        r = api_client.get(path, headers=u.headers)
        assert r.status_code == 403, r.text
    finally:
        u.drop()


@pytest.mark.parametrize("path", ACTIVITY_PATHS)
def test_activity_console_refuses_a_plain_admin(api_client, admin, path):
    """super_admin only, for the reason the feed is.

    None of `app_events` / `auth_events` / `ingest_events` has a branch column,
    so there is no honest way to scope them. "Not your page" is a better answer
    than a silently global one handed to a branch manager, and better than a
    silently empty one.
    """

    r = api_client.get(path, headers=admin.headers)
    assert r.status_code == 403, r.text


# ---- 1. timezone (§A) -------------------------------------------------------


def test_a_turn_at_local_dawn_buckets_on_the_local_day(api_client, seeded, admin):
    """01:00 Asia/Yangon is 18:30 UTC the day before. The bucket must MOVE.

    This is the defect, not a preference. Before the fix every chart bucketed
    with `date_trunc('day', ts)` in UTC while the console labelled the result in
    the browser's zone, so each "day" ran 06:30 -> 06:30 local and the first six
    and a half hours of every morning were counted on the previous day.

    The assertion is that the SAME row lands on a DIFFERENT day under a different
    `tz` — which a silent fallback to UTC cannot produce, and neither can a
    `tz` parameter that is declared and dropped.
    """

    def turns_on(day, tz):
        body = _get(api_client, "/admin/analytics/timeseries", admin.headers,
                    bucket="day", tz=tz, store=MINE)
        return {r["t"]: r["turns"] for r in body["rows"]}.get(day, 0)

    d0, d1 = _d(0), _d(1)

    utc = _get(api_client, "/admin/analytics/timeseries", admin.headers,
               bucket="day", tz="UTC", store=MINE)
    yangon = _get(api_client, "/admin/analytics/timeseries", admin.headers,
                  bucket="day", tz=YANGON, store=MINE)
    assert utc["tz"] == "UTC" and yangon["tz"] == YANGON

    utc_by_day = {r["t"]: r["turns"] for r in utc["rows"]}
    ygn_by_day = {r["t"]: r["turns"] for r in yangon["rows"]}

    # In UTC the 18:30 turn joins the 04:00 turn on D0 and D1 has nothing.
    assert utc_by_day.get(d0) == 2
    assert utc_by_day.get(d1, 0) == 0
    # In Yangon it moves to D1 — a real move, in both directions at once.
    assert ygn_by_day.get(d0) == 1
    assert ygn_by_day.get(d1) == 1
    assert turns_on(d1, YANGON) > turns_on(d1, "UTC")


def test_the_daily_cost_rollup_uses_the_same_midnight(api_client, seeded, admin):
    """`/analytics/cost?group=day` buckets in `tz` too, not only the traffic chart.

    Two panels on one page cutting their days at different midnights is worse
    than both being wrong the same way: the spend chart and the traffic chart
    stop reconciling and the discrepancy reads as a pricing bug.
    """

    utc = _get(api_client, "/admin/analytics/cost", admin.headers,
               group="day", tz="UTC", store=MINE)
    ygn = _get(api_client, "/admin/analytics/cost", admin.headers,
               group="day", tz=YANGON, store=MINE)
    utc_days = {r["key"]: r["turns"] for r in utc["rows"]}
    ygn_days = {r["key"]: r["turns"] for r in ygn["rows"]}
    assert utc_days.get(_d(0)) == 2
    assert ygn_days.get(_d(0)) == 1
    assert ygn_days.get(_d(1)) == 1


def test_a_bare_date_bound_is_read_in_the_requested_zone(api_client, seeded, admin):
    """The window moves with the zone, or it disagrees with the buckets drawn on it.

    `start=D1` in Yangon begins at 17:30 UTC on D0, which is BEFORE the 18:30
    turn — so that turn is inside the Yangon window and outside the UTC one.
    Fixing the buckets without fixing the bounds leaves the first bar of every
    chart short by exactly the offset.
    """

    utc = _get(api_client, "/admin/analytics/timeseries", admin.headers,
               bucket="day", tz="UTC", store=MINE, start=_d(1), end=_d(2))
    ygn = _get(api_client, "/admin/analytics/timeseries", admin.headers,
               bucket="day", tz=YANGON, store=MINE, start=_d(1), end=_d(2))
    assert sum(r["turns"] for r in utc["rows"]) == 0
    assert sum(r["turns"] for r in ygn["rows"]) == 1


@pytest.mark.parametrize(
    "path",
    ["/admin/analytics/timeseries", "/admin/analytics/cost",
     "/admin/analytics/economics", "/admin/analytics/llm-calls"],
)
def test_an_invalid_timezone_is_a_400_on_analytics(api_client, admin, path):
    """Never a silent fallback to UTC — that is the bug, reinstated."""

    r = api_client.get(path, params={"tz": "Mars/Olympus"}, headers=admin.headers)
    assert r.status_code == 400, r.text
    assert "tz" in r.text


@pytest.mark.parametrize("path", ACTIVITY_PATHS)
def test_an_invalid_timezone_is_a_400_on_activity(api_client, root, path):
    r = api_client.get(path, params={"tz": "Not/AZone"}, headers=root.headers)
    assert r.status_code == 400, r.text
    assert "tz" in r.text


def test_an_empty_tz_means_utc_rather_than_an_error(api_client, seeded, admin):
    """`tz=` is "unset", not "invalid". A browser that fails to resolve its own
    zone sends an empty string, and refusing it would black out the page over a
    default the contract already specifies."""

    body = _get(api_client, "/admin/analytics/timeseries", admin.headers,
                bucket="day", tz="", store=MINE)
    assert body["tz"] == "UTC"


# ---- 1b. the zone ECHO (§F1) ------------------------------------------------
#
# The sharpest point made this round, and it belongs in tests rather than in a
# comment: if an endpoint fails to DECLARE `tz`, FastAPI drops it silently and
# answers 200 with UTC buckets — under a header chip that now says "GMT+6:30",
# because the UI looks timezone-aware. That is the original bug with better
# camouflage. The UI renders its chip from the echo, so an endpoint that buckets
# and does not echo is unusable, and one that echoes something it did not apply
# is worse than one that echoes nothing.

_ANALYTICS_BUCKETING = [
    ("/admin/analytics/summary", {}),
    ("/admin/analytics/timeseries", {"bucket": "day"}),
    ("/admin/analytics/cost", {"group": "day"}),
    ("/admin/analytics/data-health", {}),
    ("/admin/analytics/llm-calls", {}),
    ("/admin/analytics/economics", {}),
]


@pytest.mark.parametrize("path,params", _ANALYTICS_BUCKETING)
def test_every_analytics_payload_echoes_the_zone_it_used(
        api_client, seeded, admin, path, params):
    r = api_client.get(path, params={"tz": YANGON, **params},
                       headers=admin.headers)
    assert r.status_code == 200, r.text
    assert r.json().get("tz") == YANGON, r.text


@pytest.mark.parametrize("path", ACTIVITY_PATHS)
def test_every_activity_payload_echoes_the_zone_it_used(
        api_client, seeded, root, path):
    body = _get(api_client, path, root.headers, tz=YANGON)
    # Top level, not only inside `window`: the chip reads the top-level key, and
    # an endpoint that forgot to declare `tz` would look identical from there.
    assert body["tz"] == YANGON
    assert body["window"]["tz"] == YANGON


@pytest.mark.parametrize("path,params", _ANALYTICS_BUCKETING)
def test_the_echo_is_utc_when_no_zone_was_asked_for(
        api_client, seeded, admin, path, params):
    """The echo reports what the QUERY used, never the raw request string.

    Omitting `tz` really does bucket in UTC, so the echo must say UTC — the UI
    then shows "buckets are UTC" in warning colour, which is the true statement.
    """

    r = api_client.get(path, params=params, headers=admin.headers)
    assert r.status_code == 200, r.text
    assert r.json().get("tz") == "UTC", r.text


# ---- the tz database has to be COMPLETE (a packaging guard) -----------------
#
# These are the zone names browsers and operating systems ACTUALLY send. Chrome
# in Yangon reports `Asia/Rangoon`, not `Asia/Yangon`; Indian machines report
# `Asia/Calcutta`; the whole `US/*` family is in daily use. They are tzdata
# LINKS — real zones, not typos.
#
# `python:3.12-slim` ships an INCOMPLETE `/usr/share/zoneinfo` where
# `Asia/Yangon` resolves and `Asia/Rangoon` does not, so the validator rejected
# the zone the user's own browser sent and every panel on the console rendered a
# 400. `tzdata` in requirements.txt is the fix.
#
# **The defect these tests catch is a PACKAGING regression, not a logic one.**
# The validator was right; its environment was wrong. Drop `tzdata` from
# requirements.txt again and these fail here, loudly, instead of the console
# going blank in somebody's browser a week later.
_REAL_WORLD_ALIASES = [
    "Asia/Rangoon", "Asia/Calcutta", "US/Eastern", "US/Pacific",
    "Europe/Kiev", "Asia/Saigon", "GB", "Etc/UTC",
]


@pytest.mark.parametrize("sent", _REAL_WORLD_ALIASES)
def test_a_legacy_zone_alias_is_accepted(api_client, seeded, admin, sent):
    """The zone the user's own browser reports must work.

    Every curl check passed while the product was unusable, because they all
    sent the canonical name. The parameter's value comes from a machine nobody
    here controls, so the test has to send what that machine sends.
    """

    r = api_client.get("/admin/analytics/timeseries",
                       params={"tz": sent, "bucket": "day"}, headers=admin.headers)
    assert r.status_code == 200, r.text
    # Echoed as sent: it IS the zone that was used, and the §F1 chip needs a
    # name to print. `UTC` and `Etc/UTC` are both understood as UTC by the UI.
    assert r.json()["tz"] == sent


@pytest.mark.parametrize("sent", _REAL_WORLD_ALIASES)
def test_aliases_are_accepted_on_the_activity_console_too(
        api_client, seeded, root, sent):
    body = _get(api_client, "/admin/activity/summary", root.headers, tz=sent)
    assert body["tz"] == sent
    assert body["window"]["tz"] == sent


def test_an_alias_is_really_applied_not_merely_accepted(api_client, seeded, admin):
    """A 200 is not enough — the buckets have to actually move.

    `Asia/Rangoon` and `Asia/Yangon` are the same zone, so they must agree
    byte-for-byte; and both must DIFFER from UTC, or this would pass just as
    well against a validator that accepted the name and quietly bucketed in UTC
    anyway — which is the exact failure the whole `tz` parameter exists to stop.
    """

    def rows(tz):
        body = _get(api_client, "/admin/analytics/timeseries", admin.headers,
                    bucket="day", tz=tz, store=MINE)
        return {r["t"]: r["turns"] for r in body["rows"]}

    assert rows("Asia/Rangoon") == rows("Asia/Yangon")
    assert rows("Asia/Rangoon") != rows("UTC")


def test_the_tz_database_dependency_is_declared():
    """`tzdata` must stay in requirements.txt.

    The tests above catch its absence only on a machine that lacks a system tz
    database — a developer box usually has one, so they can pass locally while
    the built image is broken. That is exactly how this shipped. This one fails
    deterministically, wherever it runs, the moment the dependency is dropped.
    """

    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    text = (root / "requirements.txt").read_text()
    deps = {line.strip().split("==")[0].split(">=")[0].lower()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")}
    assert "tzdata" in deps, (
        "tzdata is gone from requirements.txt — python:3.12-slim ships an "
        "incomplete /usr/share/zoneinfo and the analytics console will reject "
        "Asia/Rangoon, Asia/Calcutta and every US/* zone a browser sends."
    )


@pytest.mark.parametrize("junk", ["Mars/Olympus", "Not/AZone", "'; DROP TABLE x",
                                  "../../etc/passwd", "Asia/Yangonn"])
def test_genuine_nonsense_is_still_a_400(api_client, admin, junk):
    """The rule was never "accept anything".

    It was "never silently substitute a DIFFERENT zone", and an unknown name
    still gets the loud answer. Widening the validator to real aliases must not
    widen it to everything — that would put UTC buckets under a chip naming a
    zone that does not exist.
    """

    r = api_client.get("/admin/analytics/timeseries",
                       params={"tz": junk}, headers=admin.headers)
    assert r.status_code == 400, r.text
    assert "tz" in r.text


def test_cost_says_where_the_money_is_when_the_turn_column_is_empty(
        api_client, seeded, admin):
    """Turn-level cost is structurally NULL on this stack; the panel must say so.

    `chat_logs.cost_usd` comes from agno's aggregate `metrics.cost`, and
    OpenRouter returns no price on the completion response — so it stays NULL
    for every turn, forever, while `llm_calls.cost_usd` carries the real money
    via the per-call `usage.cost` and `_estimated_cost` fallback.

    A `—` there is indistinguishable from a quiet window and sends somebody to
    check the instrumentation that is working. So the payload distinguishes
    "tokens measured, cost not" from "nothing measured", and names where to go.
    """

    # Seeded turns deliberately carry token counts AND a turn-level cost, so
    # first prove the happy path still reports the cost it has.
    priced = _get(api_client, "/admin/analytics/cost", admin.headers,
                  group="day", store=MINE)
    assert priced["available"] is True
    assert priced["cost_available"] is True
    assert priced["cost_hint"] is None

    # Now the real-stack shape: tokens present, turn cost NULL.
    _pg("UPDATE chat_logs SET cost_usd = NULL WHERE store_id = $1"
        "   AND ts >= $2::text::timestamptz AND ts < $3::text::timestamptz",
        MINE, f"{_d(0)} 00:00:00+00", f"{_d(3)} 00:00:00+00")

    body = _get(api_client, "/admin/analytics/cost", admin.headers,
                group="day", store=MINE)
    # Still available: the TOKEN half is real and must keep charting.
    assert body["available"] is True
    assert body["cost_available"] is False
    assert all(r["cost_usd"] is None for r in body["rows"])
    assert any(r["total_tokens"] for r in body["rows"])
    # And it points at the endpoint that does have the money.
    assert body["priced_calls"] == 1          # the one priced seeded call
    assert "economics" in body["cost_hint"]


def test_a_cost_panel_with_no_data_still_echoes_its_zone(api_client, admin):
    """The empty shape carries the echo too.

    A quiet window returns `available: false` and no rows — and if the echo went
    missing with the rows, the chip would flip to "buckets are UTC" the moment
    traffic stopped, which reads as a configuration change rather than as quiet.
    """

    body = _get(api_client, "/admin/analytics/cost", admin.headers,
                group="day", tz=YANGON, store="no-such-store-at-all")
    assert body["available"] is False
    assert body["tz"] == YANGON


# ---- 1c. the ingest funnel (§F2) --------------------------------------------


def _funnel_baseline() -> Dict[str, int]:
    """Attempt counts per step as they stand right now.

    The funnel is deliberately UNWINDOWED — it answers "how has the pipeline
    done", and the numbers the contract quotes (573 arrived, 286 detected) are
    all-history. So these tests cannot isolate themselves the way the windowed
    ones do, and asserting an absolute count would assert the contents of
    whatever database the suite was cloned from.

    They assert the DIFFERENCE this fixture makes instead, which is the thing
    actually under test: that a run reaching `loaded` is counted once at every
    stage it passed and at none it did not.
    """

    rows = _pg(
        "SELECT step, count(DISTINCT run_id) AS n FROM ingest_events GROUP BY 1",
        fetch=True,
    )
    return {r["step"]: int(r["n"]) for r in rows}


@pytest.fixture
def ingested():
    """One pipeline run per shape: recognised+loaded, unrecognised, rejected."""

    _ensure_schema()
    _purge()
    before = _funnel_baseline()
    tag = uuid.uuid4().hex[:8]
    # A file that made it all the way through.
    for step in ("arrived", "detected", "checked", "loaded", "indexed",
                 "cache_cleared", "stored"):
        _ingest_event(at=f"{_d(0)} 06:00:00+00", file=f"good-{tag}.xlsx",
                      step=step, status="ok")
    # A file nobody could identify: it arrived and stopped.
    _ingest_event(at=f"{_d(0)} 07:00:00+00", file=f"mystery-{tag}.xlsx",
                  step="arrived", status="ok")
    _ingest_event(at=f"{_d(0)} 07:00:01+00", file=f"mystery-{tag}.xlsx",
                  step="unrecognised")
    # A file that was recognised and then refused by the validator.
    for step, st in (("arrived", "ok"), ("detected", "ok"), ("rejected", "bad"),
                     ("set_aside", "bad")):
        _ingest_event(at=f"{_d(1)} 06:00:00+00", file=f"bad-{tag}.xlsx",
                      step=step, status=st)
    yield {"tag": tag, "before": before}
    _purge()


def test_the_funnel_narrows_and_names_where_files_left(api_client, ingested, admin):
    """573 arrived / 286 detected is the finding, and it needs both numbers.

    Half of everything arriving is not recognised and nothing in the console said
    so. A funnel that reported only the stages files reach would show the same
    shape as a healthy pipeline with less traffic.
    """

    body = _get(api_client, "/admin/analytics/data-health", admin.headers)
    f = body["funnel"]
    was = ingested["before"]

    def added(step):
        return f[step] - was.get(step, 0)

    assert added("arrived") == 3
    assert added("detected") == 2    # the mystery file never got identified
    assert added("checked") == 1     # the bad one was refused by the validator
    assert added("loaded") == 1
    assert added("set_aside") == 1
    # It narrows. A funnel whose stages do not is unreadable, and the whole
    # finding — half of everything arriving is not recognised — is the gap
    # between the first two bars.
    assert f["arrived"] >= f["detected"] >= f["checked"] >= f["loaded"]

    meta = body["funnel_meta"]
    # Where the two missing files WENT, named rather than left to subtraction.
    drops = meta["drops"]
    assert drops["unrecognised"] - was.get("unrecognised", 0) == 1
    assert drops["rejected"] - was.get("rejected", 0) == 1
    # Counted per attempt, and it says so — a retried file is two attempts, and
    # a reader has to know which of the two readings they are looking at.
    assert meta["unit"] == "run"
    # `set_aside` is an outcome, not a narrowing stage: it can exceed `loaded`,
    # and a UI drawing it as the last bar of a descending funnel would be lying.
    assert meta["terminal"] == ["set_aside"]


def test_a_stage_nothing_reached_is_zero_and_not_absent(api_client, ingested, admin):
    """Every declared stage is present. A missing key would render as `—`
    ("we cannot see") where the truth is a measured zero."""

    body = _get(api_client, "/admin/analytics/data-health", admin.headers)
    assert set(body["funnel"]) == {"arrived", "detected", "checked", "loaded",
                                   "set_aside"}
    assert all(isinstance(v, int) for v in body["funnel"].values())


def test_the_funnel_is_null_when_the_table_cannot_be_read(api_client, admin):
    """`null`, not a funnel of zeroes.

    Zeroes would say no file has ever arrived — the most alarming wrong answer
    this panel can give, and indistinguishable from a dead SFTP drop.
    """

    _pg("ALTER TABLE ingest_events RENAME TO ingest_events_hidden")
    try:
        body = _get(api_client, "/admin/analytics/data-health", admin.headers)
        assert body["funnel"] is None
        assert body["funnel_meta"]["unit"] == "run"   # shape survives
    finally:
        _pg("ALTER TABLE ingest_events_hidden RENAME TO ingest_events")


# ---- 2. period deltas (§B) --------------------------------------------------


def test_no_prior_window_reports_null_and_not_zero(api_client, seeded, root):
    """Without both bounds there is no "same length again" — so `null`.

    `0` would say the previous period measured nothing, and `0%` would say
    nothing changed. Both are claims. The honest answer is that we were not
    asked about a previous period at all.
    """

    body = _get(api_client, "/admin/activity/summary", root.headers,
                start=None, end=None)
    assert body["window"]["prev_period"] is None
    for kpi in (body["events"], body["clients"]["browser"],
                body["failures"]["failed"], body["files_set_aside"]):
        assert kpi["prev"] is None
        assert kpi["delta"] is None, kpi
        assert kpi["delta_pct"] is None, kpi


def test_a_prior_window_produces_both_numbers(api_client, seeded, root):
    """A percentage never travels without its absolute (§B).

    The seed puts one app_event in the previous window and four in the current
    one, so the delta is +3 and the percentage is +300% — and the test asserts
    the ABSOLUTE, because "300%" over a base of one is exactly the noise the
    contract bans presenting alone.
    """

    body = _get(api_client, "/admin/activity/summary", root.headers)
    events = body["events"]
    prev = body["window"]["prev_period"]
    assert prev is not None and prev["end"] is not None
    assert events["prev"] == 2          # 1 app_event + 1 auth_event on D-1
    assert events["value"] == 7         # 4 app + 3 auth in the current window
    assert events["delta"] == 5
    assert events["delta_pct"] == 250.0
    assert events["prev_period"] == prev


def test_growth_from_a_zero_prior_has_no_percentage(api_client, seeded, root):
    """0 -> N is an absolute rise and no percentage at all.

    `delta_pct: 0` would read as "unchanged" and any other number would be
    invented. `null` is the only honest cell, and `delta` still carries the
    whole of the movement.
    """

    body = _get(api_client, "/admin/activity/summary", root.headers)
    aside = body["files_set_aside"]
    assert aside["prev"] == 0           # the pipeline set nothing aside before
    assert aside["value"] == 1
    assert aside["delta"] == 1
    assert aside["delta_pct"] is None


def test_analytics_summary_deltas_agree_with_the_headline_numbers(
        api_client, seeded, admin):
    """`deltas.<metric>.value` IS the top-level `<metric>`, for every metric.

    The two are computed by one code path precisely so they cannot disagree —
    this pins that. Two queries measuring "the same" thing is how the halves of
    a delta end up defined differently, and a delta between two definitions is
    worse than no delta because it looks like a finding.
    """

    body = _get(api_client, "/admin/analytics/summary", admin.headers, store=MINE)
    d = body["deltas"]
    for key in ("turns", "distinct", "repeat_rate", "cache_hits", "cache_rate",
                "p50_ms", "p95_ms", "cost_usd"):
        assert d[key]["value"] == body[key], key
    assert d["rated"]["value"] == body["feedback"]["rated"]
    assert d["corrections"]["value"] == body["feedback"]["corrections"]
    # `tokens` is the delta-friendly scalar; the payload keeps the object.
    assert d["tokens"]["value"] == body["tokens"]["total"]


def test_burmese_share_is_a_kpi_and_moves_with_the_lang_filter(
        api_client, seeded, admin):
    """Roughly half this product's traffic is Burmese; a shift in that split
    changes who it is failing.

    The seeded window holds one Burmese turn (the SIBLING's) among three, so an
    unscoped read is 1/3 and a `lang=MY` read is all of it. Asserting both is
    what separates a working share from a constant.
    """

    body = _get(api_client, "/admin/analytics/summary", admin.headers)
    assert body["deltas"]["burmese_share"]["value"] == round(1 / 3, 4)

    burmese = _get(api_client, "/admin/analytics/summary", admin.headers,
                   lang="MY")
    assert burmese["deltas"]["burmese_share"]["value"] == 1.0

    english = _get(api_client, "/admin/analytics/summary", admin.headers,
                   lang="EN")
    assert english["deltas"]["burmese_share"]["value"] == 0.0   # measured zero


def test_burmese_share_is_null_over_an_empty_window(api_client, seeded, admin):
    """No traffic is not a measured share of zero (§3)."""

    body = _get(api_client, "/admin/analytics/summary", admin.headers,
                store="no-such-store-at-all")
    assert body["turns"] == 0
    assert body["deltas"]["burmese_share"]["value"] is None


def test_analytics_summary_delta_block_is_present_even_with_no_prior_window(
        api_client, seeded, admin):
    """Absent block and `delta: null` mean different things to the UI.

    A missing block is "this build does not compute movement" and draws no chip;
    `{"delta": null}` is "there was nothing before this window" and prints "no
    prior period". Omitting it hides a real answer behind a capability question.
    """

    body = _get(api_client, "/admin/analytics/summary", admin.headers,
                start=None, end=None, store=MINE)
    assert body["prev_period"] is None
    assert set(body["deltas"]) >= {"turns", "cache_rate", "up_rate", "tokens"}
    for kpi in body["deltas"].values():
        assert kpi["prev"] is None
        assert kpi["delta"] is None
        assert kpi["delta_pct"] is None


def test_analytics_summary_deltas_measure_the_preceding_window(
        api_client, seeded, admin):
    """The prior window really is measured, not defaulted to zero.

    The window D0..D2 holds two of MINE's turns (the third seeded turn belongs
    to the SIBLING branch) and its predecessor D-3..D-1 holds none, so `prev` is
    a measured 0 and `delta` is the whole of `value`. A prior window that was
    never queried would show `prev: null` instead — which is why this asserts 0
    and not "falsy".
    """

    body = _get(api_client, "/admin/analytics/summary", admin.headers, store=MINE)
    turns = body["deltas"]["turns"]
    assert turns["value"] == 2
    assert turns["prev"] == 0
    assert turns["delta"] == 2
    assert turns["delta_pct"] is None       # no percentage from a zero base
    assert body["prev_period"]["end"] is not None


def test_analytics_summary_deltas_survive_the_legacy_window_spelling(
        api_client, seeded, admin):
    """`from`/`to` gets the same prior window as `start`/`end`.

    The previous window is passed to the shared helper with `from`/`to` blanked;
    forgetting that would send a legacy window AND a shifted one on the same
    call and trip the conflict guard — a 400 the caller did nothing to earn.
    """

    body = _get(api_client, "/admin/analytics/summary", admin.headers,
                start=None, end=None, store=MINE, **{"from": WINDOW_START,
                                                     "to": WINDOW_END})
    assert body["deltas"]["turns"]["value"] == 2
    assert body["deltas"]["turns"]["prev"] == 0
    assert body["prev_period"] is not None


# ---- 3. the endpoints filter, and the filters MOVE a number -----------------


def test_activity_summary_splits_browser_from_testclient(api_client, seeded, root):
    """A pytest run's own audit rows must not be counted as traffic.

    144 of them once reached a production `app_events` table and out-ranked the
    only real admin on the instance. A KPI that cannot separate them is
    measuring the test suite.
    """

    body = _get(api_client, "/admin/activity/summary", root.headers)
    assert body["clients"]["testclient"]["value"] == 1
    assert body["clients"]["browser"]["value"] == 5     # 3 app + 2 auth, real ips
    assert body["clients"]["no_client"]["value"] == 1   # the ip-less login
    # The three bands account for every event and nothing is double counted.
    assert (body["clients"]["testclient"]["value"]
            + body["clients"]["browser"]["value"]
            + body["clients"]["no_client"]["value"]) == body["events"]["value"]


def test_activity_summary_action_filter_narrows_the_count(api_client, seeded, root):
    """The declared-but-dropped-parameter check, against the unfiltered answer."""

    tag = seeded["tag"]
    everything = _get(api_client, "/admin/activity/summary", root.headers)
    narrowed = _get(api_client, "/admin/activity/summary", root.headers,
                    action=f"users.delete.{tag}")
    assert everything["events"]["value"] == 7
    assert narrowed["events"]["value"] == 1
    assert narrowed["events"]["value"] < everything["events"]["value"]


def test_activity_summary_ip_filter_narrows_the_count(api_client, seeded, root):
    everything = _get(api_client, "/admin/activity/summary", root.headers)
    only_bot = _get(api_client, "/admin/activity/summary", root.headers,
                    ip="testclient")
    assert only_bot["events"]["value"] == 1
    assert only_bot["events"]["value"] < everything["events"]["value"]


def test_a_block_that_cannot_obey_a_filter_says_so(api_client, seeded, root):
    """§5: `ingest_events` has no actor, so the KPI declares itself unfiltered.

    A number sitting under a chip it silently ignores is the same lie as an
    undeclared parameter, just further from the wire.
    """

    plain = _get(api_client, "/admin/activity/summary", root.headers)
    chipped = _get(api_client, "/admin/activity/summary", root.headers,
                   actor="nobody@example.com")
    assert plain["files_set_aside"]["filters_applied"] is True
    assert chipped["files_set_aside"]["filters_applied"] is False


def test_activity_trends_series_is_zero_filled_and_movers_rank_by_change(
        api_client, seeded, root):
    """A quiet bucket is a visible zero, and a mover carries both numbers.

    Zero-fill is not cosmetic: `GROUP BY` omits an empty bucket, and every chart
    library draws a straight line from the point before it to the point after —
    so an outage renders as steady traffic.
    """

    tag = seeded["tag"]
    body = _get(api_client, "/admin/activity/trends", root.headers, rollup="day")
    days = {r["t"]: r["events"] for r in body["series"]}
    assert days.get(_d(0)) == 3
    assert days.get(_d(1)) == 4
    assert days.get(_d(2)) == 0          # materialised, not missing
    assert set(days) == {_d(0), _d(1), _d(2)}

    movers = {m["key"]: m for m in body["movers"]}
    assert f"settings.update.{tag}" in movers
    up = movers[f"settings.update.{tag}"]
    assert up["n"] == 3 and up["prev"] == 1
    assert up["delta"] == 2 and up["delta_pct"] == 200.0
    assert [p["t"] for p in up["spark"]] == [_d(0), _d(1)]


def test_activity_summary_reports_failures_by_exact_code(api_client, seeded, root):
    """A class says something failed; the code says which conversation to have.

    403 is an authorisation wall and 422 is a file the validator turned away.
    Rolled into `4xx` they are indistinguishable and they lead to completely
    different places.
    """

    body = _get(api_client, "/admin/activity/summary", root.headers)
    assert body["failures"]["by_status"] == {"200": 3, "403": 1}
    # The class rollup still agrees with the codes it rolled up.
    classes = {c["class"]: c["n"] for c in body["failures"]["classes"]}
    assert classes["2xx"] == 3 and classes["4xx"] == 1
    assert body["failures"]["rate"] == {"rate": 0.25, "n": 4}


def test_activity_summary_counts_distinct_actors(api_client, seeded, root):
    body = _get(api_client, "/admin/activity/summary", root.headers)
    # one-, two-, bot- across app_events; the sign-ins reuse one- and two-.
    assert body["distinct_actors"]["value"] == 3
    assert body["distinct_actors"]["prev"] == 1


def test_activity_trends_carries_the_previous_window_as_a_ghost_line(
        api_client, seeded, root):
    """Aligned by POSITION and cut to the current series' length.

    The previous window's buckets carry different dates, so joining on the label
    would match nothing and draw an empty line; and a ghost one point longer
    than the chart under it renders as a spike at the edge.
    """

    body = _get(api_client, "/admin/activity/trends", root.headers, rollup="day")
    prev = body["previous"]
    assert len(prev["values"]) == len(body["series"])
    assert sum(prev["values"]) == 2          # the two events seeded into D-1
    assert prev["label"] == "previous period"


def test_activity_trends_ghost_line_is_empty_without_a_prior_window(
        api_client, seeded, root):
    """No window pinned, no previous period — and an empty list, not zeroes.

    A row of zeroes would draw a flat line along the axis and read as "there was
    no activity before this", which is a measurement nobody took.
    """

    body = _get(api_client, "/admin/activity/trends", root.headers,
                start=None, end=None, rollup="day")
    assert body["previous"]["values"] == []
    assert body["previous"]["label"] is None


def test_trends_previous_is_cut_for_the_SELECTED_measure(api_client, seeded, root):
    """A `failed` line against an `events` ghost compares two different things.

    It also looks entirely plausible doing it, which is why the measure is a
    declared parameter rather than something the frontend picks after the fact.
    The seed has 7 events and 1 failure in the window, 2 events and 0 failures
    before it — so the two ghosts are different numbers and a measure that was
    ignored would show the wrong one.
    """

    events = _get(api_client, "/admin/activity/trends", root.headers,
                  rollup="day", measure="events")
    failed = _get(api_client, "/admin/activity/trends", root.headers,
                  rollup="day", measure="failed")

    assert events["measure"] == "events"
    assert failed["measure"] == "failed"
    assert events["previous"]["measure"] == "events"
    assert failed["previous"]["measure"] == "failed"

    assert sum(events["previous"]["values"]) == 2   # both prior-window events
    assert sum(failed["previous"]["values"]) == 0   # neither was a failure
    # And the two really are different series, not the same one relabelled.
    assert events["previous"]["values"] != failed["previous"]["values"]


@pytest.mark.parametrize("bad", ["cost", "count(*)", "spend", "1"])
def test_trends_rejects_an_unknown_measure(api_client, root, bad):
    r = api_client.get("/admin/activity/trends", params={"measure": bad},
                       headers=root.headers)
    assert r.status_code == 400, r.text
    assert "`measure`" in r.text


def test_trends_heatmap_uses_the_callers_hours(api_client, seeded, root):
    """Hour-of-day is cut server-side, in the request's zone.

    Deriving it in the browser from a bucket label means re-parsing a timestamp
    whose zone handling is exactly what this round fixed — the same defect one
    layer further out. The seeded 03:00 UTC event is 09:30 in Yangon, so the
    column it lands in must MOVE with `tz`.
    """

    utc = _get(api_client, "/admin/activity/trends", root.headers,
               rollup="day", tz="UTC")["heatmap"]
    ygn = _get(api_client, "/admin/activity/trends", root.headers,
               rollup="day", tz=YANGON)["heatmap"]

    # Hour-of-day as COLS (a fixed 0..23), days as ROWS. Fixed, so an hour
    # nothing landed in is still a column and two days stay comparable at the
    # same x position.
    assert [c["key"] for c in utc["cols"]] == [str(h) for h in range(24)]
    assert [r["key"] for r in utc["rows"]] == [_d(0), _d(1), _d(2)]

    def hour_for(hm, day, hour):
        row = next(r for r in hm["rows"] if r["key"] == day)
        return row["cells"][hour]["value"]

    # D0 holds three events across all sources — the sign-in at 02:00 and the
    # two admin actions at 03:00 and 03:30 UTC. In Yangon those are 08:30, 09:30
    # and 10:00. Hourly resolution shows exactly where each one went, which is
    # the whole reason this is not four six-hour bands.
    assert hour_for(utc, _d(0), 2) == 1
    assert hour_for(utc, _d(0), 3) == 2
    assert hour_for(ygn, _d(0), 8) == 1
    assert hour_for(ygn, _d(0), 9) == 1
    assert hour_for(ygn, _d(0), 10) == 1
    # Nothing is left behind in the UTC hours once the zone moves.
    assert hour_for(ygn, _d(0), 2) == 0 and hour_for(ygn, _d(0), 3) == 0
    # The count is conserved across the shift, not merely shuffled.
    assert (sum(c["value"] for r in utc["rows"] for c in r["cells"])
            == sum(c["value"] for r in ygn["rows"] for c in r["cells"]))

    # An hour with no events is a measured 0, not a gap: the query looked.
    assert hour_for(utc, _d(2), 14) == 0


def test_explore_sub_stacks_without_changing_the_table(api_client, seeded, root):
    """`sub` splits each bucket into parts; the table still summarises BUCKETS.

    Reading the cells raw with `sub` set would count a bucket once per part —
    `n` would report parts as buckets and min/max would describe the parts
    rather than the days. So the table must come back identical either way.
    """

    plain = _get(api_client, "/admin/activity/explore", root.headers,
                 by="source", rollup="day")
    stacked = _get(api_client, "/admin/activity/explore", root.headers,
                   by="source", sub="action", rollup="day")

    assert plain["sub"] is None
    assert stacked["sub"] == "action"
    # Identical on every shared field — `sub` adds the per-row truncation
    # markers and changes nothing it summarises.
    shared = {"key", "n", "rows", "min", "max", "avg", "sum", "share"}
    assert ([{k: v for k, v in r.items() if k in shared} for r in stacked["table"]]
            == plain["table"])
    # The per-row marker sits on the key that lost its tail, so the table can
    # say "top 5 of 12 sources" instead of a page-level "something was cut".
    app_row = next(r for r in stacked["table"] if r["key"] == "app")
    assert app_row["sub_of"] == 2          # settings.update + users.delete
    assert app_row["sub_truncated"] is False
    assert stacked["sub_top"] == 5

    # The series really did split.
    assert all("sub" not in r for r in plain["series"])
    assert all("sub" in r for r in stacked["series"])
    app_parts = {r["sub"] for r in stacked["series"] if r["key"] == "app"}
    assert len(app_parts) >= 2          # settings.update AND users.delete
    # Parts of one bucket sum to that bucket's unstacked value.
    day0 = sum(r["value"] for r in stacked["series"]
               if r["key"] == "app" and r["t"] == _d(0))
    assert day0 == next(r["value"] for r in plain["series"]
                        if r["key"] == "app" and r["t"] == _d(0))


def test_explore_rejects_a_sub_that_is_not_a_dimension(api_client, root):
    r = api_client.get("/admin/activity/explore",
                       params={"by": "action", "sub": "cost_usd"},
                       headers=root.headers)
    assert r.status_code == 400 and "`sub`" in r.text


def test_explore_rejects_subgrouping_a_dimension_by_itself(api_client, root):
    """One part per key and a stacked chart identical to the unstacked one.

    That reads as a bug in the chart rather than as a bad request, so it is
    refused with a reason instead of answered with something useless.
    """

    r = api_client.get("/admin/activity/explore",
                       params={"by": "action", "sub": "action"},
                       headers=root.headers)
    assert r.status_code == 400 and "differ" in r.text


def test_explore_serves_its_own_picker_options(api_client, seeded, root):
    """The menu and the whitelist come from one map, so they cannot disagree.

    A picker offering an option that 400s on click is a worse failure than a
    shorter picker — it teaches the reader the page is broken.
    """

    body = _get(api_client, "/admin/activity/explore", root.headers)
    measures = [o["key"] for o in body["options"]["measures"]]
    dims = [o["key"] for o in body["options"]["dimensions"]]
    assert set(measures) == {"events", "status", "duration_ms"}
    assert set(dims) == {"actor", "action", "source", "target", "ip",
                         "status_class"}
    assert all(o["label"] for o in body["options"]["dimensions"])

    # The real assertion: every advertised option is actually accepted.
    for m in measures:
        for d in dims:
            r = api_client.get("/admin/activity/explore",
                               params={"start": WINDOW_START, "end": WINDOW_END,
                                       "measure": m, "by": d},
                               headers=root.headers)
            assert r.status_code == 200, f"{m}/{d}: {r.text}"


def test_files_set_aside_carries_its_denominator(api_client, ingested, root):
    """"190 set aside" is a number nobody can act on; "190 of 573" is."""

    body = _get(api_client, "/admin/activity/summary", root.headers)
    aside = body["files_set_aside"]
    assert aside["value"] == 1
    assert aside["arrived"] == 3
    assert aside["of_arrived"] == {"rate": round(1 / 3, 4), "n": 3}
    # Numerator and denominator count the SAME thing — a rate that mixes units
    # is worse than no rate.
    assert aside["unit"] == "run"


def test_activity_audit_counts_403s_by_action_with_distinct_actors(
        api_client, seeded, root):
    """One account refused forty times and forty refused once are the same `n`."""

    tag = seeded["tag"]
    body = _get(api_client, "/admin/activity/audit", root.headers)
    forbidden = {r["action"]: r for r in body["forbidden"]}
    assert forbidden[f"users.delete.{tag}"]["n"] == 1
    assert forbidden[f"users.delete.{tag}"]["actors"] == 1
    assert f"settings.update.{tag}" not in forbidden      # those were 200s

    signins = {r["t"]: r for r in body["signins"]}
    assert signins[_d(0)]["login_ok"] == 1
    assert signins[_d(1)]["login_fail"] == 2
    assert signins[_d(2)]["attempts"] == 0                # measured, not missing


# ---- 4. the pivot refuses what it does not recognise (§C) -------------------


@pytest.mark.parametrize("bad", ["actor; DROP TABLE users", "password", "e.ip)--",
                                 "1", ""])
def test_explore_rejects_an_unknown_dimension(api_client, root, bad):
    """`by` is a KEY into a map, never a column name. Anything else is a 400.

    This endpoint is the one place in the file where a caller names something
    column-shaped, so the whitelist is the design rather than a formality: an
    interpolated `by` is SQL injection with a query parameter as the vector.
    """

    r = api_client.get("/admin/activity/explore", params={"by": bad},
                       headers=root.headers)
    assert r.status_code == 400, r.text
    assert "`by`" in r.text


@pytest.mark.parametrize("bad", ["cost_usd", "count(*)", "1=1", ""])
def test_explore_rejects_an_unknown_measure(api_client, root, bad):
    r = api_client.get("/admin/activity/explore", params={"measure": bad},
                       headers=root.headers)
    assert r.status_code == 400, r.text
    assert "`measure`" in r.text


def test_explore_rejects_an_unknown_rollup(api_client, root):
    r = api_client.get("/admin/activity/explore", params={"rollup": "fortnight"},
                       headers=root.headers)
    assert r.status_code == 400, r.text


def test_explore_pivots_and_its_shares_carry_the_denominator(
        api_client, seeded, root):
    """The table summarises the SERIES, so `rollup` changes the answer.

    Per day, `settings.update` ran twice on D0 and once on D1: two buckets, min
    1, max 2, sum 3. Rolled up to a month it is ONE bucket of 3 — which is the
    same total and a different shape, and a pivot whose rollup did not reach the
    table would report both identically.
    """

    tag = seeded["tag"]
    daily = _get(api_client, "/admin/activity/explore", root.headers,
                 by="action", measure="events", rollup="day", top=10)
    rows = {r["key"]: r for r in daily["table"]}
    up = rows[f"settings.update.{tag}"]
    assert up["n"] == 2 and up["rows"] == 3
    assert up["min"] == 1.0 and up["max"] == 2.0 and up["sum"] == 3.0
    assert up["avg"] == 1.5
    # A rate never ships without its denominator (§3).
    assert up["share"]["n"] == 7.0
    assert round(up["share"]["rate"], 4) == round(3 / 7, 4)

    monthly = _get(api_client, "/admin/activity/explore", root.headers,
                   by="action", measure="events", rollup="month", top=10)
    m_up = {r["key"]: r for r in monthly["table"]}[f"settings.update.{tag}"]
    assert m_up["n"] == 1 and m_up["sum"] == 3.0 and m_up["max"] == 3.0


def test_explore_by_status_class_keeps_the_unmeasured_band(api_client, seeded, root):
    """A row with no HTTP status is a NULL key, not a `2xx`.

    auth and ingest rows were never HTTP. Folding them into a success class
    would flatter the failure rate with events that could not have failed.
    """

    body = _get(api_client, "/admin/activity/explore", root.headers,
                by="status_class", measure="events", rollup="day", top=10)
    keys = {r["key"]: r for r in body["table"]}
    assert keys["2xx"]["sum"] == 3.0
    assert keys["4xx"]["sum"] == 1.0
    assert None in keys and keys[None]["sum"] == 3.0


def test_explore_measures_duration_without_counting_the_rows_that_lack_one(
        api_client, seeded, root):
    """A row with no duration contributes to `rows` and to nothing else.

    The three sources are UNIONed, and only `app_events` carries a duration —
    an auth row has none. Coercing that absence to `0` would drag every average
    towards zero and make the slowest actions look fastest. The cast is also
    regex-guarded rather than bare: one unparseable value in a JSONB field
    assembled from three tables would otherwise 500 the whole panel.
    """

    tag = seeded["tag"]
    body = _get(api_client, "/admin/activity/explore", root.headers,
                by="actor", measure="duration_ms", rollup="day", top=10)
    rows = {r["key"]: r for r in body["table"]}

    one = rows[f"one-{tag}@corp.mm"]
    assert one["rows"] == 3          # 2 app events + 1 sign-in
    assert one["n"] == 1             # one day that had a measurable duration
    assert one["sum"] == 42.0        # 12 + 30, and the sign-in adds nothing

    # The testclient row has a NULL duration and is the only event for its actor.
    bot = rows[f"bot-{tag}@corp.mm"]
    assert bot["rows"] == 1
    assert bot["n"] == 0
    assert bot["sum"] is None        # unmeasured, not zero
    assert bot["avg"] is None


def test_explore_says_when_it_truncated_the_ranking(api_client, seeded, root):
    """"The top 10" of exactly 10 and of 400 are different facts."""

    one = _get(api_client, "/admin/activity/explore", root.headers,
               by="action", rollup="day", top=1)
    assert len(one["table"]) == 1
    assert one["truncated"] is True

    everything = _get(api_client, "/admin/activity/explore", root.headers,
                      by="action", rollup="day", top=50)
    assert len(everything["table"]) > 1
    assert everything["truncated"] is False


def test_explore_source_filter_narrows_the_table(api_client, seeded, root):
    everything = _get(api_client, "/admin/activity/explore", root.headers,
                      by="source", rollup="day")
    only_auth = _get(api_client, "/admin/activity/explore", root.headers,
                     by="source", rollup="day", source="auth")
    assert {r["key"] for r in everything["table"]} == {"app", "auth"}
    assert {r["key"] for r in only_auth["table"]} == {"auth"}


# ---- 5. store scope is a boundary, not a filter -----------------------------


@pytest.mark.parametrize("path", ANALYTICS_PATHS)
def test_a_pinned_caller_cannot_widen_scope_with_store(api_client, seeded,
                                                       pinned, admin, path):
    """`?store=<sibling>` from a branch-pinned caller narrows to nothing.

    The enforced scope comes from the caller's own users row and the `store`
    filter is ANDed with it, never substituted for it. The unpinned admin sees
    the sibling's data on the same request, which is what proves the pinned
    caller's empty answer is the boundary holding rather than an empty window.
    """

    unpinned = _get(api_client, path, admin.headers, store=SIBLING)
    scoped = _get(api_client, path, pinned.headers, store=SIBLING)

    if path.endswith("llm-calls"):
        assert unpinned["total"] == 1            # the sibling's one model call
        assert scoped["total"] == 0
        assert scoped["rows"] == []
    else:
        assert unpinned["calls"] == 1
        assert scoped["calls"] == 0
        assert scoped["cost_usd"] is None


def test_a_pinned_caller_still_sees_their_own_branch(api_client, seeded, pinned):
    """The negative control's positive half: the boundary is not just "empty"."""

    body = _get(api_client, "/admin/analytics/llm-calls", pinned.headers)
    assert body["total"] == 2                    # both of MINE's calls
    assert {r["store_id"] for r in body["rows"]} == {MINE}


# ---- 6. llm-calls: the per-call grain (§D) ----------------------------------


def test_llm_calls_returns_one_row_per_call_with_the_cache_split(
        api_client, seeded, admin):
    """Per TURN the cache split is one number and the lever is invisible.

    Turn `a` holds a call with 250 cached tokens and one with none. Averaged to
    the turn that is a single figure nobody can act on; per call it is the
    difference between the two rows.
    """

    body = _get(api_client, "/admin/analytics/llm-calls", admin.headers,
                store=MINE)
    assert body["total"] == 2
    by_model = {r["model"]: r for r in body["rows"]}
    assert by_model["test/priced"]["cache_read_tokens"] == 250
    assert by_model["test/unpriced"]["cache_read_tokens"] == 0
    assert {r["turn_id"] for r in body["rows"]} == {seeded["a"]}
    assert sorted(r["seq"] for r in body["rows"]) == [0, 1]


def test_an_unpriced_call_costs_null_and_not_zero(api_client, seeded, admin):
    """A `0.0` reads as "free" and nobody notices for months (§3)."""

    body = _get(api_client, "/admin/analytics/llm-calls", admin.headers,
                store=MINE)
    by_model = {r["model"]: r for r in body["rows"]}
    assert by_model["test/unpriced"]["cost_usd"] is None
    assert by_model["test/priced"]["cost_usd"] == 0.1
    # And the total says how much of itself it can vouch for.
    assert body["totals"]["priced_calls"] == 1
    assert body["totals"]["cost_coverage"] == {"rate": 0.5, "n": 2}


def test_llm_calls_model_filter_narrows_and_applies_to_the_call(
        api_client, seeded, admin):
    """`model` filters `llm_calls.model`, not the turn's headline model.

    Both seeded calls belong to a turn whose `chat_logs.model` is
    `test/turn-model`, so a filter that reached the turn instead would return
    both rows or none — never exactly one.
    """

    everything = _get(api_client, "/admin/analytics/llm-calls", admin.headers,
                      store=MINE)
    one = _get(api_client, "/admin/analytics/llm-calls", admin.headers,
               store=MINE, model="test/unpriced")
    assert everything["total"] == 2
    assert one["total"] == 1
    assert one["rows"][0]["model"] == "test/unpriced"


def test_order_turn_keeps_one_turn_s_calls_together_and_in_sequence(
        api_client, seeded, admin):
    """`order=turn` is the ordering the intra-turn spread needs; `ts` is not.

    Under `ts` two calls of one turn are adjacent only while no other turn's
    call falls between their timestamps — and under any concurrency one will.
    The turn_id tiebreaker cannot save it: it applies only when the timestamps
    are exactly equal. So the reading that makes §D's point (seq 2 cheap and
    cached, seq 4 fifteen times dearer and not) has to be asked for explicitly.

    Within a turn the sequence runs FORWARDS, so the rows read in the order the
    calls actually ran.
    """

    body = _get(api_client, "/admin/analytics/llm-calls", admin.headers,
                store=MINE, order="turn")
    rows = body["rows"]
    assert [r["seq"] for r in rows] == [0, 1]
    assert len({r["turn_id"] for r in rows}) == 1

    # Newest turn first across turns, so the page still opens on recent work.
    both = _get(api_client, "/admin/analytics/llm-calls", admin.headers,
                order="turn", start=WINDOW_START, end=WINDOW_END)
    ids = [r["turn_id"] for r in both["rows"]]
    assert ids == sorted(ids, reverse=True)
    # Contiguous: each turn's rows form ONE unbroken run. A turn id reappearing
    # after a different one is exactly the interleaving `ts` permits.
    seen = set()
    for n, i in enumerate(ids):
        if n == 0 or i != ids[n - 1]:
            assert i not in seen, "a turn's calls were split by another turn's"
            seen.add(i)


def test_llm_calls_rejects_an_unknown_order(api_client, admin):
    r = api_client.get("/admin/analytics/llm-calls", params={"order": "cost); --"},
                       headers=admin.headers)
    assert r.status_code == 400, r.text


# ---- 7. economics: every ratio with its denominator -------------------------


def test_economics_reports_each_ratio_with_its_denominator(
        api_client, seeded, admin):
    """"$2.14 per million" over four calls and over forty thousand are the same
    number and different facts. The denominator is the half somebody plans a
    budget on."""

    body = _get(api_client, "/admin/analytics/economics", admin.headers)

    # 0.1 + 0.2 over 3,000 + 100 priced tokens.
    assert body["cost_usd"] == 0.3
    assert body["priced_calls"] == 2
    assert body["unpriced_calls"] == 1
    assert body["cost_is_estimated"] is False       # both prices were reported
    blended = body["blended_per_1m_usd"]
    assert blended["n"] == 3100
    assert blended["denominator"] == "tokens on priced calls"
    assert round(blended["value"]) == round(0.3 * 1_000_000 / 3100)

    assert body["cost_per_turn_usd"]["n"] == 2      # two turns had a model call
    assert body["cost_per_call_usd"]["n"] == 2      # priced calls, not all calls

    # cache read is measured against PROMPT tokens — the only thing it can be
    # read from. Against total tokens the largest lever would look like a minor.
    assert body["cache_read_share"] == {"rate": round(1250 / 3500, 4), "n": 3500}
    assert body["completion_share"]["n"] == 3610
    assert body["prompt_completion_ratio"]["n"] == 110
    assert body["prompt_completion_ratio"]["denominator"] == "completion tokens"


def test_economics_never_reports_zero_cost_for_an_unpriced_window(
        api_client, seeded, admin):
    """Filtered to the unpriced model alone, spend is `null` — not `0.0`.

    A zero says this window was free. It was not measured, which is a different
    thing, and the one the operator needs to see before quoting a number.
    """

    body = _get(api_client, "/admin/analytics/economics", admin.headers,
                model="test/unpriced")
    assert body["calls"] == 1
    assert body["priced_calls"] == 0
    assert body["cost_usd"] is None
    assert body["blended_per_1m_usd"]["value"] is None
    assert body["blended_per_1m_usd"]["n"] == 0
    assert body["cost_per_turn_usd"]["value"] is None


def test_economics_lang_filter_moves_the_numbers(api_client, seeded, admin):
    """The Burmese turn is the sibling's; filtering to it must change the cost."""

    everything = _get(api_client, "/admin/analytics/economics", admin.headers)
    burmese = _get(api_client, "/admin/analytics/economics", admin.headers,
                   lang="MY")
    assert everything["calls"] == 3
    assert burmese["calls"] == 1
    assert burmese["cost_usd"] == 0.2
    assert burmese["cost_usd"] != everything["cost_usd"]


# ---- 8. section isolation (§6) ----------------------------------------------


def test_a_missing_source_table_costs_one_block_not_the_page(
        api_client, seeded, root):
    """A summary over a database with no `ingest_events` keeps its shape.

    The empty value must be shaped like the real one — same keys, same types —
    because the frontend must never branch on a missing key.
    """

    _pg("ALTER TABLE ingest_events RENAME TO ingest_events_hidden")
    try:
        body = _get(api_client, "/admin/activity/summary", root.headers)
        aside = body["files_set_aside"]
        assert set(aside) >= {"value", "prev", "delta", "delta_pct",
                              "prev_period", "filters_applied"}
        assert aside["value"] is None          # cannot say, rather than zero
        assert body["events"]["value"] == 7    # the rest of the page survived
    finally:
        _pg("ALTER TABLE ingest_events_hidden RENAME TO ingest_events")
