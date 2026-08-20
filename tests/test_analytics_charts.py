"""Chart endpoints + the merged activity feed: the numbers, the NULLs, the pin.

This file covers the four plotting endpoints added over ``chat_logs``
(``/analytics/timeseries|tools|paths|cost``), the two additive fields on
``/analytics/summary``, and ``/admin/activity``. Four things are pinned, in
descending order of how bad it is to get them wrong:

1. **A branch-pinned admin must not read another branch's questions.** These
   endpoints aggregate the same rows ``/analytics/questions`` returns verbatim,
   and an aggregate leaks too: a tool name, a route, a spend figure and a daily
   count are all facts about another pharmacy's traffic. Every new endpoint is
   therefore tested with the decoy technique from ``test_tools_scope`` /
   ``test_analytics``: ``200059-CCZZ`` CONTAINS the scope token ``20005`` but is
   a different branch, so a substring matcher fails these tests and only an
   anchored one passes. Without the decoy the tests could not tell the two apart
   — the vacuous-guard trap CLAUDE.md records.

2. **An absence is never reported as a measurement.** A day with no traffic is a
   zero-filled bucket with a NULL latency (not a missing point a chart library
   joins straight through, and not 0 ms). A turn with no recorded ``path`` or
   ``tools`` stays NULL and is counted as ``not_recorded``. Uncaptured tokens
   make ``cost.available`` false and ``summary.tokens`` null — never 0, which
   would claim the traffic was free.

3. **The activity feed degrades loudly.** With one table missing it returns the
   others AND names the one it could not read; "no activity" and "half the audit
   trail is invisible" must not look the same.

4. **The feed is super_admin only.** A plain admin gets 403, not a filtered view.

Needs live Postgres, like the rest of the suite.

⚠️ Every DB setup/teardown goes through :func:`_pg`, a throwaway asyncpg
connection — NOT ``app.db.q`` — for the loop-affinity reason
``tests/test_analytics.py`` documents.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest import mock

import pytest

from app import admin as adminmod
from app import auth as authmod
from app.config import get_settings

from tests.pgconn import pg

# Imported at collection time, not inside a test: app.api pulls in agno, which
# builds an asyncio.Lock() at import (see test_approval.py).
from app.api import app as _app  # noqa: F401


def _pg(query: str, *args, fetch: bool = False):
    """Run one statement on a private connection. Never touches app.db's pool.

    One connection per PROCESS, not per statement — see tests/pgconn.py for why
    the previous arrangement was the suite's whole wall clock.
    """

    return pg(query, *args, fetch=fetch)


def _ensure_schema():
    """chat_logs with the audit AND turn-metric columns, without the app's pool.

    Mirrors ``app.admin.ensure_chat_logs`` including the ALTERs — which are the
    point: this database was created long before ``migrations/0006_turn_metrics``
    existed, so without the ALTER every cost assertion below would fail on a
    missing column rather than on a real defect.
    """

    _pg("ALTER TABLE users ADD COLUMN IF NOT EXISTS store_id TEXT")
    _pg(
        """CREATE TABLE IF NOT EXISTS chat_logs (
               id BIGSERIAL PRIMARY KEY,
               ts TIMESTAMPTZ DEFAULT now(),
               lang TEXT, store_id TEXT,
               question TEXT, answer TEXT,
               cached BOOLEAN, latency_ms INT
           )"""
    )
    for col, typ in (
        ("embed_id", "TEXT"),
        ("session_id", "TEXT"),
        ("model", "TEXT"),
        ("tools", "JSONB"),
        ("path", "TEXT"),
        ("input_tokens", "INT"),
        ("output_tokens", "INT"),
        ("total_tokens", "INT"),
        ("reasoning_tokens", "INT"),
        ("cost_usd", "NUMERIC(12,6)"),
        ("ttft_ms", "INT"),
    ):
        _pg(f"ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS {col} {typ}")


def _ensure_event_tables():
    """auth_events + app_events, both idempotent.

    ``app_events`` is owned by ``app/activity.py``; created here too so this file
    runs against a database whose lifespan has never started that module.
    """

    _pg(
        """CREATE TABLE IF NOT EXISTS auth_events (
               id BIGSERIAL PRIMARY KEY,
               ts TIMESTAMPTZ DEFAULT now(),
               event TEXT NOT NULL, email TEXT, actor_email TEXT,
               ip TEXT, detail TEXT
           )"""
    )
    _pg(
        """CREATE TABLE IF NOT EXISTS app_events (
               id BIGSERIAL PRIMARY KEY,
               ts TIMESTAMPTZ DEFAULT now(),
               actor_email TEXT, actor_role TEXT,
               action TEXT, target TEXT,
               method TEXT, path TEXT, status INT,
               detail JSONB, ip TEXT, duration_ms INT
           )"""
    )


# ---- seeding ---------------------------------------------------------------

# Marker carried by every seeded row, so each query can narrow to exactly this
# run's rows with `q=`. The dev database holds real traffic and an aggregate over
# "whatever is in the table" asserts nothing.
MARK = f"zzchart{uuid.uuid4().hex[:10]}"

MINE = "20005-CCYK"
SIBLING = "20024-CC73"
# Contain a scope token but are DIFFERENT branches: `200059-CCZZ` contains
# `20005`, `20099-CCYKX` contains `CCYK`. One decoy per token form, or the
# parametrisation that has no decoy cannot fail.
DECOY = "200059-CCZZ"
DECOY_SUFFIX = "20099-CCYKX"
DECOYS = (DECOY, DECOY_SUFFIX)

# The decoy branches are the ONLY seeded rows carrying tokens and a cost. A
# scoped admin must therefore see `cost.available == false`; a substring matcher
# hands them another pharmacy's spend.
DECOY_TOKENS = (111, 222, 333)
DECOY_COST = "0.012345"


def _insert(**kw):
    _pg(
        """INSERT INTO chat_logs
               (ts, lang, store_id, question, answer, cached, latency_ms,
                embed_id, session_id, model, tools, path,
                input_tokens, output_tokens, total_tokens, reasoning_tokens,
                cost_usd, ttft_ms)
           VALUES ($1::text::timestamptz,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12,
                   $13,$14,$15,$16,$17::text::numeric,$18)""",
        kw["ts"],
        kw.get("lang", "EN"),
        kw.get("store_id"),
        kw["question"],
        kw.get("answer", "an answer"),
        kw.get("cached", False),
        kw.get("latency_ms"),
        kw.get("embed_id"),
        kw.get("session_id"),
        kw.get("model"),
        kw.get("tools"),
        kw.get("path"),
        kw.get("input_tokens"),
        kw.get("output_tokens"),
        kw.get("total_tokens"),
        kw.get("reasoning_tokens"),
        kw.get("cost_usd"),
        kw.get("ttft_ms"),
    )


def _purge():
    _pg("DELETE FROM chat_logs WHERE question LIKE $1", f"%{MARK}%")


@pytest.fixture
def seeded_charts():
    """Three turns at MINE with a DELIBERATE EMPTY DAY, plus sibling/decoy rows.

    MINE:
      2026-08-10  latency 1000, cached, tools ["get_stock"],                path agent
      2026-08-10  latency 3000,         tools ["get_stock","find_at_other"], path agent
      2026-08-12  latency 5000,         tools NULL,                          path NULL

    2026-08-11 has NO row at MINE — that hole is what the zero-fill test reads,
    and it is also where every sibling/decoy row sits, so a leak shows up as the
    hole being filled with somebody else's traffic.
    """

    _ensure_schema()
    _purge()
    _insert(ts="2026-08-10T09:00:00Z", store_id=MINE, question=f"{MARK} q0",
            cached=True, latency_ms=1000, tools='["get_stock"]', path="agent",
            model="google/gemini-3.5-flash", embed_id="embA")
    _insert(ts="2026-08-10T09:30:00Z", store_id=MINE, question=f"{MARK} q1",
            latency_ms=3000, tools='["get_stock", "find_at_other_stores"]',
            path="agent", model="google/gemini-3.5-flash", embed_id="embA")
    _insert(ts="2026-08-12T09:00:00Z", store_id=MINE, question=f"{MARK} q2",
            latency_ms=5000, tools=None, path=None, model=None, embed_id=None)

    _insert(ts="2026-08-11T10:00:00Z", store_id=SIBLING,
            question=f"{MARK} sibling secret question", latency_ms=7000,
            tools='["sibling_tool"]', path="sibling_path",
            model="sibling/model", embed_id="embSIB",
            input_tokens=DECOY_TOKENS[0], output_tokens=DECOY_TOKENS[1],
            total_tokens=DECOY_TOKENS[2], cost_usd=DECOY_COST)
    for n, site in enumerate(DECOYS):
        _insert(ts="2026-08-11T10:01:00Z", store_id=site,
                question=f"{MARK} decoy secret question {n}", latency_ms=9000,
                tools='["decoy_tool"]', path="decoy_path",
                model="decoy/model", embed_id=f"embDECOY{n}",
                input_tokens=DECOY_TOKENS[0], output_tokens=DECOY_TOKENS[1],
                total_tokens=DECOY_TOKENS[2], cost_usd=DECOY_COST)
    yield {"mark": MARK, "mine": MINE}
    _purge()


class _Admin:
    """An approved account + a bearer header, optionally pinned to a branch."""

    def __init__(self, role="admin", store_id=None):
        _ensure_schema()
        self.email = f"chart-{uuid.uuid4().hex[:10]}@corp.mm"
        rows = _pg(
            """INSERT INTO users (email, name, role, auth_sources, active, approved, store_id)
               VALUES ($1,'Charts',$2,ARRAY['local'],TRUE,TRUE,$3)
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
    a = _Admin()
    yield a
    a.drop()


@pytest.fixture
def superadmin():
    a = _Admin(role="super_admin")
    yield a
    a.drop()


def _get(client, path, headers, **params):
    r = client.get(path, params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---- 1. timeseries: bucketing, zero fill, percentiles ----------------------


def test_timeseries_zero_fills_the_empty_day(api_client, seeded_charts, admin):
    """The gap on the 11th must be a POINT with turns 0, not a missing row.

    Absent, every chart library joins the 10th straight to the 12th and draws
    steady traffic across a day that had none. `p50_ms` on that bucket stays
    NULL — a day nobody was served has no median latency, and 0 would read as
    instant.
    """

    body = _get(api_client, "/admin/analytics/timeseries", admin.headers,
                q=MARK, store=MINE, **{"from": "2026-08-10", "to": "2026-08-12"})
    assert body["bucket"] == "day"
    assert [r["t"] for r in body["rows"]] == ["2026-08-10", "2026-08-11", "2026-08-12"]

    gap = body["rows"][1]
    assert gap["turns"] == 0 and gap["cached"] == 0 and gap["refusals"] == 0
    assert gap["p50_ms"] is None and gap["p95_ms"] is None
    assert gap["tokens"] is None and gap["cost_usd"] is None


def test_timeseries_counts_and_percentiles(api_client, seeded_charts, admin):
    """percentile_cont over the two latencies on the 10th: 1000 and 3000.

    rank 0.5*(n-1) = 0.5  -> midpoint                 = 2000
    rank 0.95*(n-1) = 0.95 -> 1000 + 0.95*(3000-1000) = 2900
    """

    rows = _get(api_client, "/admin/analytics/timeseries", admin.headers,
                q=MARK, store=MINE, **{"from": "2026-08-10", "to": "2026-08-12"})["rows"]
    first, last = rows[0], rows[2]
    assert first["turns"] == 2 and first["cached"] == 1
    assert first["p50_ms"] == 2000 and first["p95_ms"] == 2900
    assert last["turns"] == 1 and last["p50_ms"] == 5000
    assert sum(r["turns"] for r in rows) == 3


def test_timeseries_hour_bucket_keeps_the_whole_last_day(api_client, seeded_charts, admin):
    """A bare `to` date means the WHOLE day, hourly too.

    Truncating '2026-08-10' to a bucket bound would end the axis at 00:00 and
    drop all 24 hours of it — the same midnight trap `_log_filters` documents.
    """

    body = _get(api_client, "/admin/analytics/timeseries", admin.headers,
                bucket="hour", q=MARK, store=MINE,
                **{"from": "2026-08-10", "to": "2026-08-10"})
    assert body["bucket"] == "hour"
    assert len(body["rows"]) == 24                 # the whole day, hour by hour
    busy = [r for r in body["rows"] if r["turns"]]
    # Both seeded rows land in ONE hour; which hour depends on the server's
    # timezone, so the assertion is on the day and the count, not on 09:00.
    assert len(busy) == 1 and busy[0]["turns"] == 2
    assert busy[0]["t"].startswith("2026-08-10")
    assert sum(r["turns"] for r in body["rows"]) == 2   # the rest zero-filled


def test_timeseries_rejects_an_unknown_bucket(api_client, admin):
    r = api_client.get("/admin/analytics/timeseries", params={"bucket": "week"},
                       headers=admin.headers)
    assert r.status_code == 400
    assert "bucket" in r.json()["detail"]


# ---- 2. tools: the unnest, and what was never recorded ---------------------


def test_tools_unnest_counts_calls_and_turns(api_client, seeded_charts, admin):
    body = _get(api_client, "/admin/analytics/tools", admin.headers, q=MARK, store=MINE)
    rows = {r["tool"]: r for r in body["rows"]}
    assert set(rows) == {"get_stock", "find_at_other_stores"}
    assert rows["get_stock"]["calls"] == 2 and rows["get_stock"]["turns"] == 2
    assert rows["find_at_other_stores"]["calls"] == 1
    assert rows["get_stock"]["p50_ms"] == 2000       # median of 1000, 3000
    assert body["total_turns_with_tools"] == 2


def test_tools_reports_how_much_history_predates_capture(api_client, seeded_charts, admin):
    """`not_recorded` counts turns whose `tools` is NULL.

    One seeded turn has none. Counting it as "used no tools" would let the page
    imply the agent answered bare when the truth is nobody wrote it down — the
    page has to be able to say how much of the window it cannot speak for.
    """

    body = _get(api_client, "/admin/analytics/tools", admin.headers, q=MARK, store=MINE)
    assert body["not_recorded"] == 1
    assert "not_recorded" not in {r["tool"] for r in body["rows"]}


# ---- 3. paths: NULL is a value ---------------------------------------------


def test_paths_keeps_null_distinct_from_a_real_route(api_client, seeded_charts, admin):
    """A NULL `path` comes back as null, never folded into 'agent'.

    'agent' is the plausible guess for an old row (it was the only route then),
    and it is exactly the guess that would make this chart unreproducible from
    the table it claims to summarise.
    """

    rows = _get(api_client, "/admin/analytics/paths", admin.headers, q=MARK, store=MINE)
    by_path = {r["path"]: r for r in rows}
    assert set(by_path) == {"agent", None}
    assert by_path["agent"]["turns"] == 2
    assert by_path[None]["turns"] == 1
    assert by_path[None]["p50_ms"] == 5000


# ---- 4. cost: `available` is computed, not assumed -------------------------


def test_cost_is_unavailable_when_no_turn_has_tokens(api_client, seeded_charts, admin):
    """No seeded MINE row carries tokens, so the panel must veil itself.

    Rows of zeros would say "these turns were free". They were not measured.
    """

    body = _get(api_client, "/admin/analytics/cost", admin.headers, q=MARK, store=MINE)
    assert body["available"] is False
    assert body["reason"] == "no turn has token data yet"
    assert body["rows"] == []


def test_cost_becomes_available_once_one_row_has_tokens(api_client, seeded_charts, admin):
    """One priced turn flips `available` and the sums are that turn's."""

    _insert(ts="2026-08-12T11:00:00Z", store_id=MINE, question=f"{MARK} priced",
            latency_ms=4000, path="agent", model="google/gemini-3.5-flash",
            input_tokens=10, output_tokens=20, total_tokens=30, cost_usd="0.000750")
    body = _get(api_client, "/admin/analytics/cost", admin.headers, q=MARK, store=MINE)
    assert body["available"] is True
    day = {r["key"]: r for r in body["rows"]}["2026-08-12"]
    assert day["input_tokens"] == 10 and day["output_tokens"] == 20
    assert day["total_tokens"] == 30
    assert day["cost_usd"] == pytest.approx(0.00075)
    # The unpriced turns are still counted as turns — they happened.
    assert day["turns"] == 2

    by_model = _get(api_client, "/admin/analytics/cost", admin.headers,
                    group="model", q=MARK, store=MINE)
    assert by_model["available"] is True
    keys = {r["key"] for r in by_model["rows"]}
    assert "google/gemini-3.5-flash" in keys
    assert None in keys                      # the model-less turn keeps a NULL key


def test_cost_rejects_an_unknown_group(api_client, admin):
    r = api_client.get("/admin/analytics/cost", params={"group": "embed"},
                       headers=admin.headers)
    assert r.status_code == 400


# ---- 5. summary: additive, and null rather than zero -----------------------


def test_summary_tokens_and_cost_are_null_when_uncaptured(api_client, seeded_charts, admin):
    s = _get(api_client, "/admin/analytics/summary", admin.headers, q=MARK, store=MINE)
    assert s["turns"] == 3                       # the existing shape is unchanged
    assert s["tokens"] is None
    assert s["cost_usd"] is None                 # NOT 0 — nothing was measured


def test_summary_tokens_appear_once_captured(api_client, seeded_charts, admin):
    _insert(ts="2026-08-12T11:00:00Z", store_id=MINE, question=f"{MARK} priced",
            latency_ms=4000, path="agent", input_tokens=10, output_tokens=20,
            total_tokens=30, cost_usd="0.000750")
    s = _get(api_client, "/admin/analytics/summary", admin.headers, q=MARK, store=MINE)
    assert s["tokens"] == {"input": 10, "output": 20, "total": 30}
    assert s["cost_usd"] == pytest.approx(0.00075)


# ---- 6. store scoping — the decoy tests ------------------------------------
#
# `200059-CCZZ` contains `20005` and `20099-CCYKX` contains `CCYK`, and both are
# other pharmacies. Their rows sit on 2026-08-11 (the day MINE has none) and
# carry tool names, routes, models and a cost that MINE has nowhere — so a
# substring matcher does not merely inflate a count, it puts another branch's
# spend on the page.


@pytest.mark.parametrize("token_form", ["20005", "CCYK", "20005-CCYK"])
def test_timeseries_scope_does_not_substring_match_a_sibling(
    api_client, seeded_charts, token_form
):
    a = _Admin(store_id=token_form)
    try:
        rows = _get(api_client, "/admin/analytics/timeseries", a.headers, q=MARK,
                    **{"from": "2026-08-10", "to": "2026-08-12"})["rows"]
        by_day = {r["t"]: r for r in rows}
        # The 11th is the sibling/decoy day. It must stay EMPTY.
        assert by_day["2026-08-11"]["turns"] == 0
        assert sum(r["turns"] for r in rows) == 3
        assert all(r["cost_usd"] is None for r in rows)
    finally:
        a.drop()


@pytest.mark.parametrize("token_form", ["20005", "CCYK"])
def test_tools_scope_does_not_substring_match_a_sibling(
    api_client, seeded_charts, token_form
):
    a = _Admin(store_id=token_form)
    try:
        body = _get(api_client, "/admin/analytics/tools", a.headers, q=MARK)
        tools = {r["tool"] for r in body["rows"]}
        assert tools == {"get_stock", "find_at_other_stores"}
        assert "decoy_tool" not in tools and "sibling_tool" not in tools
        assert body["total_turns_with_tools"] == 2
        assert body["not_recorded"] == 1
    finally:
        a.drop()


@pytest.mark.parametrize("token_form", ["20005", "CCYK"])
def test_paths_scope_does_not_substring_match_a_sibling(
    api_client, seeded_charts, token_form
):
    a = _Admin(store_id=token_form)
    try:
        rows = _get(api_client, "/admin/analytics/paths", a.headers, q=MARK)
        assert {r["path"] for r in rows} == {"agent", None}
        assert sum(r["turns"] for r in rows) == 3
    finally:
        a.drop()


@pytest.mark.parametrize("token_form", ["20005", "CCYK"])
def test_cost_scope_does_not_substring_match_a_sibling(
    api_client, seeded_charts, token_form
):
    """The decoys are the only priced rows, so a leak turns `available` TRUE."""

    a = _Admin(store_id=token_form)
    try:
        body = _get(api_client, "/admin/analytics/cost", a.headers, q=MARK)
        assert body["available"] is False, "another branch's spend reached this page"
        assert body["rows"] == []
    finally:
        a.drop()


@pytest.mark.parametrize("token_form", ["20005", "CCYK"])
def test_summary_tokens_scope_does_not_substring_match_a_sibling(
    api_client, seeded_charts, token_form
):
    a = _Admin(store_id=token_form)
    try:
        s = _get(api_client, "/admin/analytics/summary", a.headers, q=MARK)
        assert s["turns"] == 3
        assert s["tokens"] is None and s["cost_usd"] is None
    finally:
        a.drop()


# ---- 7. the activity feed --------------------------------------------------


@pytest.fixture
def seeded_events():
    """One app_events row and one auth_events row, both carrying MARK."""

    _ensure_event_tables()
    _purge_events()
    _pg(
        """INSERT INTO app_events
               (ts, actor_email, actor_role, action, target, method, path, status,
                detail, ip, duration_ms)
           VALUES ($1::text::timestamptz,$2,'super_admin',$3,$4,'PUT',$4,200,
                   $5::jsonb,'10.0.0.1',42)""",
        "2026-08-12T10:00:00Z", f"boss-{MARK}@corp.mm",
        f"auth-config updated {MARK}", f"/admin/auth-config/{MARK}",
        '{"ldap_enabled": true}',
    )
    _pg(
        """INSERT INTO auth_events (ts, event, email, actor_email, ip, detail)
           VALUES ($1::text::timestamptz,'login_fail',$2,NULL,'10.0.0.9',$3)""",
        "2026-08-11T10:00:00Z", f"{MARK}@corp.mm", f"bad password {MARK}",
    )
    yield MARK
    _purge_events()


def _purge_events():
    _pg("DELETE FROM app_events WHERE action LIKE $1", f"%{MARK}%")
    _pg("DELETE FROM auth_events WHERE email LIKE $1", f"%{MARK}%")


def test_activity_requires_super_admin(api_client, admin):
    """A plain admin gets 403 — not an empty feed, and not a filtered one.

    Between them these tables carry every email somebody tried to sign in as and
    every admin action's target; that is a security log, and "you may not read
    it" is the honest answer rather than a page that quietly shows less.
    """

    r = api_client.get("/admin/activity", headers=admin.headers)
    assert r.status_code == 403


def test_activity_merges_both_tables_newest_first(api_client, seeded_events, superadmin):
    body = _get(api_client, "/admin/activity", superadmin.headers, q=MARK)
    assert body["total"] == 2
    assert [r["source"] for r in body["rows"]] == ["app", "auth"]  # 12th, then 11th
    assert set(body["sources"]["included"]) >= {"app", "auth"}

    app_row, auth_row = body["rows"]
    assert app_row["actor"] == f"boss-{MARK}@corp.mm"
    assert app_row["action"] == f"auth-config updated {MARK}"
    assert app_row["target"] == f"/admin/auth-config/{MARK}"
    assert app_row["status"] == 200
    assert app_row["ip"] == "10.0.0.1"
    assert app_row["detail"]["ldap_enabled"] is True
    assert app_row["detail"]["method"] == "PUT"

    assert auth_row["action"] == "login_fail"
    assert auth_row["actor"] == f"{MARK}@corp.mm"   # no actor_email -> the account
    assert auth_row["status"] is None               # auth events have no HTTP status
    assert auth_row["detail"]["detail"] == f"bad password {MARK}"


def test_activity_filters_and_total(api_client, seeded_events, superadmin):
    """Filters narrow, and `total` is the unpaged count of what they matched."""

    only_auth = _get(api_client, "/admin/activity", superadmin.headers, q=MARK, source="auth")
    assert only_auth["total"] == 1
    assert {r["source"] for r in only_auth["rows"]} == {"auth"}

    by_actor = _get(api_client, "/admin/activity", superadmin.headers,
                    q=MARK, actor=f"boss-{MARK}")
    assert by_actor["total"] == 1 and by_actor["rows"][0]["source"] == "app"

    by_action = _get(api_client, "/admin/activity", superadmin.headers,
                     q=MARK, action="login_fail")
    assert by_action["total"] == 1 and by_action["rows"][0]["source"] == "auth"

    windowed = _get(api_client, "/admin/activity", superadmin.headers, q=MARK,
                    **{"from": "2026-08-12"})
    assert windowed["total"] == 1 and windowed["rows"][0]["source"] == "app"

    # `total` ignores limit/offset — that is what makes the pager honest.
    paged = _get(api_client, "/admin/activity", superadmin.headers, q=MARK, limit=1)
    assert paged["total"] == 2 and len(paged["rows"]) == 1
    page2 = _get(api_client, "/admin/activity", superadmin.headers, q=MARK,
                 limit=1, offset=1)
    assert page2["total"] == 2 and page2["rows"][0]["source"] == "auth"


def test_activity_degrades_when_a_table_is_absent(api_client, seeded_events, superadmin):
    """A missing table drops its source and is NAMED, not silently swallowed.

    The source list is patched to point `app` at a table that does not exist —
    non-destructively, rather than dropping the real one. `to_regclass` must see
    it is missing and the feed must still return the auth half; a blanket
    `except` around the UNION would instead return "no activity" for both, which
    is the most dangerous empty state this endpoint has.
    """

    patched = tuple(
        (name, "app_events_absent_zz" if name == "app" else table, sql)
        for name, table, sql in adminmod._ACT_SOURCES
    )
    with mock.patch.object(adminmod, "_ACT_SOURCES", patched):
        body = _get(api_client, "/admin/activity", superadmin.headers, q=MARK)

    assert body["sources"]["unavailable"] == ["app"]
    assert body["sources"]["included"] == ["auth"]
    assert body["total"] == 1
    assert [r["source"] for r in body["rows"]] == ["auth"]


# ---- 8. the chart drill-down: ?tool= and ?path= on the row queries ----------
#
# The console has always sent these two and drawn a removable "filtered by"
# chip. `/analytics/questions` never DECLARED them, and FastAPI drops an
# undeclared query param without a word — so the endpoint answered with the
# whole list while the UI said it was narrowed. That is the `sector` bug this
# codebase already shipped once, and the reason it survived a green suite is
# that nothing asserted the filter NARROWED anything; a 200 is not evidence.
#
# Every test below therefore asserts three things together: the excluded rows
# are ABSENT, the rows that should remain are PRESENT, and `total` SHRANK.


@pytest.fixture
def seeded_drill(seeded_charts):
    """`seeded_charts` plus the rows a drill-down has to tell apart.

    On top of the three MINE turns already there (q0/q1 tools ["get_stock"]
    path agent on the 10th, q2 tools NULL path NULL on the 12th):

      d1  12th  tools ["search_by_meaning"]              path fast_path  lang MM
      d2  13th  tools ["get_stock","search_by_meaning"]  path cache      cached
      d3  13th  tools ["get_stock_history"]              path agent
      d4  14th  tools ["get_stock"]                      path 'none'  (LITERAL)

    d3 is the substring decoy for the TOOL filter: `get_stock` is a prefix of
    `get_stock_history`, so a LIKE over the serialised array — or a match on the
    JSON text — returns it. Only element containment excludes it.

    d4 is the decoy for the NULL-path selector: `?path=none` is the reserved
    token for "not recorded", and it must return q2 (path IS NULL) and NOT this
    row, whose path is the three-character string.

    The sibling/decoy branches get a `get_stock` / `agent` row of their own, on
    the 11th. Without it a scope test under `?tool=get_stock` would pass for the
    wrong reason — the filter, not the boundary, would be doing the excluding.
    """

    _insert(ts="2026-08-12T12:00:00Z", store_id=MINE, question=f"{MARK} d1",
            lang="MM", latency_ms=2000, tools='["search_by_meaning"]',
            path="fast_path")
    _insert(ts="2026-08-13T09:00:00Z", store_id=MINE, question=f"{MARK} d2",
            latency_ms=100, cached=True,
            tools='["get_stock", "search_by_meaning"]', path="cache")
    _insert(ts="2026-08-13T10:00:00Z", store_id=MINE, question=f"{MARK} d3",
            latency_ms=6000, tools='["get_stock_history"]', path="agent")
    _insert(ts="2026-08-14T09:00:00Z", store_id=MINE, question=f"{MARK} d4",
            latency_ms=7000, tools='["get_stock"]', path="none")

    for site in (SIBLING, DECOY, DECOY_SUFFIX):
        _insert(ts="2026-08-11T11:00:00Z", store_id=site,
                question=f"{MARK} leak secret {site}", latency_ms=8000,
                tools='["get_stock", "sibling_tool"]', path="agent")
    return {"mark": MARK, "mine": MINE}


def _drill(client, url, headers, **params):
    """`_get` with the URL out of the way of the `path` FILTER.

    ``_get(client, path, headers, **params)`` names its URL argument ``path``,
    which is also the name of the filter under test — passing ``path="agent"``
    there hits the URL, not the query string.
    """

    r = client.get(url, params={"q": MARK, **params}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _questions(client, headers, **params):
    body = _drill(client, "/admin/analytics/questions", headers, **params)
    return body, {r["question"].split()[-1] for r in body["rows"]}


def test_questions_unfiltered_baseline(api_client, seeded_drill, admin):
    """The number every narrowing assertion below is measured against."""

    body, tags = _questions(api_client, admin.headers, store=MINE)
    assert body["total"] == 7
    assert tags == {"q0", "q1", "q2", "d1", "d2", "d3", "d4"}


def test_questions_tool_filter_narrows(api_client, seeded_drill, admin):
    """`?tool=get_stock` returns the turns whose tools ARRAY CONTAINS it.

    Asserting only a 200 here is what let the original defect ship. The filter
    has to be shown to REMOVE rows: `d1` (a different tool), `d3` (the
    `get_stock_history` substring decoy) and `q2` (tools NULL) must all be gone,
    and `total` must fall from 7 to 4.
    """

    body, tags = _questions(api_client, admin.headers, store=MINE, tool="get_stock")
    assert tags == {"q0", "q1", "d2", "d4"}
    assert body["total"] == 4 < 7
    assert "d3" not in tags, "`get_stock_history` matched a `get_stock` filter"
    assert all("get_stock" in r["tools"] for r in body["rows"])


def test_questions_tool_filter_excludes_a_null_tools_turn(api_client, seeded_drill, admin):
    """A turn whose `tools` is NULL matches NO tool filter.

    Nobody recorded what that turn called — it predates the audit columns, or it
    was a cache hit that ran no model at all. Sweeping it into a tool's drill-down
    would put a turn behind a bar it was never counted in (`/analytics/tools`
    reports it as `not_recorded`, not as a call).
    """

    for tool in ("get_stock", "search_by_meaning", "get_stock_history"):
        body, tags = _questions(api_client, admin.headers, store=MINE, tool=tool)
        assert "q2" not in tags, f"the tools-NULL turn matched ?tool={tool}"
        assert body["total"] > 0            # the filter itself still works


def test_questions_path_filter_narrows(api_client, seeded_drill, admin):
    body, tags = _questions(api_client, admin.headers, store=MINE, path="agent")
    assert tags == {"q0", "q1", "d3"}
    assert body["total"] == 3 < 7
    assert all(r["path"] == "agent" for r in body["rows"])

    fast, fast_tags = _questions(api_client, admin.headers, store=MINE, path="fast_path")
    assert fast_tags == {"d1"} and fast["total"] == 1


def test_questions_path_none_selects_exactly_the_unrecorded_turns(
    api_client, seeded_drill, admin
):
    """`?path=none` is the "not recorded" band, and it is NOT the string 'none'.

    q2 has `path IS NULL`; d4 has the literal three-character path. The reserved
    token must return the first and never the second — conflating them would make
    the one segment on the chart that means "we cannot say" indistinguishable
    from a route that happened to be named after it.
    """

    body, tags = _questions(api_client, admin.headers, store=MINE, path="none")
    assert tags == {"q2"}
    assert body["total"] == 1
    assert body["rows"][0]["path"] is None
    assert "d4" not in tags, "a literal path 'none' was folded into path IS NULL"

    # And the NULL row is not reachable by asking for a real route either.
    for real in ("agent", "fast_path", "cache"):
        _, other = _questions(api_client, admin.headers, store=MINE, path=real)
        assert "q2" not in other


def test_questions_tool_and_path_compose_with_each_other_and_the_rest(
    api_client, seeded_drill, admin
):
    """Every filter ANDs. Each step below removes something the last one kept."""

    _, both = _questions(api_client, admin.headers, store=MINE,
                         tool="get_stock", path="agent")
    assert both == {"q0", "q1"}            # d2 is path=cache, d4 is path='none'

    _, mm = _questions(api_client, admin.headers, store=MINE,
                       tool="search_by_meaning", lang="MM")
    assert mm == {"d1"}                    # d2 has the tool but is lang EN

    _, windowed = _questions(api_client, admin.headers, store=MINE, tool="get_stock",
                             **{"from": "2026-08-13", "to": "2026-08-13"})
    assert windowed == {"d2"}

    _, none_of_it = _questions(api_client, admin.headers, store=MINE,
                               tool="search_by_meaning", path="agent")
    assert none_of_it == set()


def test_questions_unknown_tool_or_path_returns_nothing_not_everything(
    api_client, seeded_drill, admin
):
    """The failure mode this endpoint had: a filter it cannot honour returning all.

    An unknown value is a legitimate question ("did anything use this?") and the
    honest answer is an empty page with `total: 0`.
    """

    for params in ({"tool": "no_such_tool_zz"}, {"path": "no_such_path_zz"},
                   {"tool": "get_stock", "path": "no_such_path_zz"}):
        body, tags = _questions(api_client, admin.headers, store=MINE, **params)
        assert body["rows"] == [] and body["total"] == 0, params
        assert tags == set()


@pytest.mark.parametrize("token_form", ["20005", "CCYK", "20005-CCYK"])
def test_questions_scope_holds_while_a_tool_filter_is_applied(
    api_client, seeded_drill, token_form
):
    """A branch-pinned admin drilling into a tool still sees only their branch.

    `200059-CCZZ` CONTAINS `20005` and `20099-CCYKX` contains `CCYK`, and both
    are other pharmacies whose turns also called `get_stock` on the `agent`
    route — so the tool filter cannot be what excludes them. Only the anchored
    `_site_clause` can. A substring matcher hands this admin another branch's
    verbatim customer questions, which is the worst row in this system to leak.
    """

    a = _Admin(store_id=token_form)
    try:
        body, tags = _questions(api_client, a.headers, tool="get_stock")
        assert tags == {"q0", "q1", "d2", "d4"}
        assert body["total"] == 4
        assert not any("leak secret" in r["question"] for r in body["rows"])
        assert {r["store_id"] for r in body["rows"]} == {MINE}

        by_path, path_tags = _questions(api_client, a.headers, path="agent")
        assert path_tags == {"q0", "q1", "d3"} and by_path["total"] == 3
    finally:
        a.drop()


def test_summary_honours_the_same_drill_down_as_the_list(api_client, seeded_drill, admin):
    """The KPI row above the list must not disagree with the list.

    A chart filtered by tool under a headline count that is not is the same lie
    in a different place — the operator reads "4 turns" off the table and "7
    turns" off the tile and has no way to know which is answering their question.
    """

    whole = _drill(api_client, "/admin/analytics/summary", admin.headers, store=MINE)
    drilled = _drill(api_client, "/admin/analytics/summary", admin.headers,
                    store=MINE, tool="get_stock")
    assert whole["turns"] == 7
    assert drilled["turns"] == 4

    by_path = _drill(api_client, "/admin/analytics/summary", admin.headers,
                    store=MINE, path="none")
    assert by_path["turns"] == 1


def test_repeats_and_embeds_honour_the_drill_down(api_client, seeded_drill, admin):
    """The other two lists the console re-loads on a drill-down.

    They take the same `params()` object as the questions list, so an undeclared
    parameter is the same silent lie on those panels.
    """

    embeds = _drill(api_client, "/admin/analytics/embeds", admin.headers,
                   store=MINE, tool="get_stock")
    # q0/q1 carry embA; d2/d4 carry none. No other embed used get_stock here.
    assert {r["embed_id"] for r in embeds} == {"embA", None}
    assert sum(r["turns"] for r in embeds) == 4

    fast = _drill(api_client, "/admin/analytics/embeds", admin.headers,
                 store=MINE, path="fast_path")
    assert sum(r["turns"] for r in fast) == 1

    repeats = _drill(api_client, "/admin/analytics/repeats", admin.headers,
                    store=MINE, tool="no_such_tool_zz")
    assert repeats == []
