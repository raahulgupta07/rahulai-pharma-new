"""The instrumented analytics endpoints: filters, store scope, and honesty.

Covers `/admin/analytics/{tool-outcomes,llm-usage,trace,diagnosis,actors,intents}`
(docs/ANALYTICS_CONTRACT.md §4–§7). Four things are load-bearing and each has a
test that FAILS if it regresses, not merely one that passes today:

1. **The filter actually filters.** FastAPI drops an undeclared query parameter
   without a word, and the endpoint then answers 200 with unfiltered data while
   the console draws a filter chip over it. A test asserting "200 and some rows"
   cannot tell that apart from a working filter, so every filter test here
   asserts that a filter value NARROWS the result to a known-different answer.

2. **A scoped caller cannot widen their scope by passing `store`.** The scope is
   an enforced boundary read from the caller's own users row; `store` is the
   operator's filter box. They are ANDed, so a pinned caller who asks for a
   sibling branch gets nothing — not the sibling.

3. **Unconfigured cost is `null`, never `0.0`.** A zero reads as "free".

4. **`refused` is not `failed`.** A tool that correctly declines and a tool that
   crashed must never land in the same bucket.

Authorisation is exercised as the LEAST-privileged caller that can reach the
page — a branch-pinned `admin` — and never as a super_admin. An admin-only
authz test cannot see a scope leak, because the super_admin scope is "all".

⚠️ Like the rest of the suite this needs live Postgres, and every setup/teardown
goes through :func:`_pg`, a throwaway asyncpg connection — NOT ``app.db.q``,
whose pool is bound to the TestClient's portal loop (see test_admin_scope.py).

⚠️ The seeded turns are timestamped into a per-process window (`WINDOW_START`..
`WINDOW_END`) and every request passes that window as `start`/`end`. That is what
isolates these assertions from the ~122 real turns in the development database,
and from a second copy of this file running at the same time: without it, "the
filter returned 3 rows" would depend on whatever else the database happens to
hold.

⚠️ That window is in the FUTURE, and it has to be. A window in the past is inside
`prune_chat_logs`' reach — it deletes anything older than
`chat_log_retention_days`, and it runs from the app lifespan, which the
`api_client` fixture starts. Seeded at 2019 the rows vanished mid-fixture and the
next INSERT died on the foreign key, which reads as a broken test rather than as
retention doing its job.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date, timedelta

import pytest

from app import auth as authmod
from app.config import get_settings

from tests.pgconn import pg

# Imported at collection time: app.api pulls in agno, which builds an
# asyncio.Lock() at import (see test_approval.py).
from app.api import app as _app  # noqa: F401

# A four-day window nothing real can be in, at a random offset inside the year
# 2099 and therefore DIFFERENT for every pytest process.
#
# A fixed window looked fine and was not. Two runs of this file at once — easily
# done while several agents work on one database — seed the same four days, and
# every count assertion doubles. It failed as "the lang filter is broken", which
# is the wrong thing to go and debug. The offset is per-process, and the fixture
# additionally purges its own window before seeding so a killed run cannot strand
# rows into the next one (the lesson `test_admin_scope._purge_decoys` records).
_RUN_DAY = date(2099, 1, 1) + timedelta(
    days=(uuid.uuid4().int + os.getpid()) % 3000
)
_D = [(_RUN_DAY + timedelta(days=i)).isoformat() for i in range(5)]
WINDOW_START = _D[0]
# EXCLUSIVE per the contract (§4), so it is the day AFTER the last seeded turn.
WINDOW_END = _D[4]

MINE = "20005-CCYK"
SIBLING = "20024-CC73"


def _pg(query: str, *args, fetch: bool = False):
    """Run one statement on a private connection. Never touches app.db's pool.

    One connection per PROCESS, not per statement — see tests/pgconn.py for why
    the previous arrangement was the suite's whole wall clock.
    """

    return pg(query, *args, fetch=fetch)


def _ensure_schema():
    """The contract's §1 schema, created here so these tests do not depend on a
    migration having been run against the developer's database.

    Statement-for-statement the same DDL as `migrations/0008_turn_calls.sql` and
    `0009_chat_logs_actor.sql`, and idempotent, so it is a no-op once those have
    been applied. If it ever drifts from them, that drift IS the bug — the
    endpoints are written against these columns.
    """

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
    # The rated filter joins on this FK, not on question/answer text (§5 amended).
    _pg("ALTER TABLE chat_feedback ADD COLUMN IF NOT EXISTS turn_id BIGINT"
        " REFERENCES chat_logs(id) ON DELETE CASCADE")


def _turn(*, ts, store, question, answer, lang="EN", path="agent",
          model="test/turn-model", cached=False, latency=1000,
          actor=None, role=None, gave_up=None, embed=None) -> int:
    rows = _pg(
        """INSERT INTO chat_logs
               (ts, store_id, question, answer, lang, path, model, cached,
                latency_ms, actor_email, actor_role, gave_up, embed_id)
           VALUES ($1::text::timestamptz,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
           RETURNING id""",
        ts, store, question, answer, lang, path, model, cached, latency,
        actor, role, gave_up, embed, fetch=True,
    )
    return rows[0]["id"]


def _tool(turn_id, seq, name, outcome, *, duration=None, error=None):
    _pg(
        """INSERT INTO tool_calls (turn_id, seq, name, outcome, duration_ms,
                                   error_message, arguments)
           VALUES ($1,$2,$3,$4,$5,$6,'{}'::jsonb)""",
        turn_id, seq, name, outcome, duration, error,
    )


def _llm(turn_id, seq, model, *, prompt=None, completion=None, cache_read=None,
         cache_creation=None, cost=None, estimated=False, ttft=None):
    _pg(
        """INSERT INTO llm_calls (turn_id, seq, model, prompt_tokens,
                                  completion_tokens, cache_read_tokens,
                                  cache_creation_tokens, cost_usd,
                                  cost_is_estimated, ttft_ms)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8::text::numeric,$9,$10)""",
        turn_id, seq, model, prompt, completion, cache_read, cache_creation,
        None if cost is None else str(cost), estimated, ttft,
    )


def _purge_window():
    """Delete anything already sitting in THIS process's window.

    Teardown deletes by id, which a killed run never reaches — and a stranded
    turn makes the NEXT run fail on a count assertion somewhere unrelated, where
    it reads as a broken filter. Purging on the way IN means a run inherits a
    clean slate instead of trusting the previous one to have exited politely.
    The window is per-process, so this cannot disturb a concurrent run.
    """

    _pg(
        "DELETE FROM chat_logs WHERE ts >= $1::text::timestamptz"
        "                        AND ts <  $2::text::timestamptz",
        WINDOW_START, WINDOW_END,
    )


@pytest.fixture
def seeded():
    """Four turns in the fixed window, spanning every case these tests need.

    * ``a`` — my branch, EN: one succeeded tool AND one REFUSED tool, one LLM
      call with NO cost configured.
    * ``b`` — the SIBLING branch, EN: a FAILED tool, an LLM call with an
      ESTIMATED cost. It exists so a scope leak has something to leak.
    * ``c`` — my branch, MY (Burmese): no tool_calls and no llm_calls at all —
      the pre-instrumentation / cache-hit shape that must be counted as
      ``not_recorded`` and never folded into a bucket.
    * ``d`` — my branch: a failed tool AND ``gave_up`` — the ``both`` row, which
      the diagnosis queue must rank as its own kind of problem.
    """

    _ensure_schema()
    _purge_window()
    tag = uuid.uuid4().hex[:8]

    a = _turn(ts=f"{_D[0]} 10:00:00+00", store=MINE,
              question=f"do you have stock of {tag}", answer="yes",
              actor=f"a-{tag}@corp.mm", role="admin")
    _tool(a, 0, "get_stock", "succeeded", duration=100)
    _tool(a, 1, "get_dosage", "refused", duration=20)
    _llm(a, 0, "test/model-a", prompt=10, completion=5, cache_read=2,
         cache_creation=1, cost=None, ttft=300)

    b = _turn(ts=f"{_D[1]} 10:00:00+00", store=SIBLING,
              question=f"price how much for {tag}", answer="k1000",
              actor=f"b-{tag}@corp.mm", role="admin")
    _tool(b, 0, "get_stock", "failed", duration=50, error="boom")
    _llm(b, 0, "test/model-b", prompt=20, completion=7, cost="0.500000",
         estimated=True, ttft=900)

    # No model: a cache hit ran none. Giving it the same model string as the
    # others would make the `model` filter test unable to fail.
    c = _turn(ts=f"{_D[2]} 10:00:00+00", store=MINE, lang="MY", model=None,
              question="ဆေး ရှိလား", answer="ရှိပါတယ်", cached=True, path="cache")

    d = _turn(ts=f"{_D[3]} 10:00:00+00", store=MINE,
              question=f"which branch has {tag}", answer="sorry, I could not",
              gave_up=True)
    _tool(d, 0, "find_at_other_stores", "failed", duration=40, error="timeout")

    ids = {"a": a, "b": b, "c": c, "d": d, "tag": tag}
    yield ids
    _pg("DELETE FROM chat_logs WHERE id = ANY($1::bigint[])", [a, b, c, d])


class _Admin:
    """An approved account + bearer header, optionally pinned to a branch."""

    def __init__(self, role="admin", store_id=None):
        _ensure_schema()
        self.email = f"an-{uuid.uuid4().hex[:10]}@corp.mm"
        rows = _pg(
            """INSERT INTO users (email, name, role, auth_sources, active,
                                  approved, store_id)
               VALUES ($1,'Analytics',$2,ARRAY['local'],TRUE,TRUE,$3)
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

    a = _Admin()
    yield a
    a.drop()


@pytest.fixture
def pinned():
    """A branch manager: role `admin`, pinned to MINE. Nothing weaker exists."""

    a = _Admin(store_id=MINE)
    yield a
    a.drop()


def _get(client, endpoint, headers, **params):
    """GET with the seeded window always applied, so the DB's real turns are out.

    The endpoint is named `endpoint`, not `path`: `path` is one of the shared
    query parameters, and a positional called `path` collides with it the moment
    a test tries to exercise that filter.
    """

    p = {"start": WINDOW_START, "end": WINDOW_END}
    p.update(params)
    r = client.get(f"/admin/analytics/{endpoint}", params=p, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _bars(body):
    return {b["name"]: b for b in body["bars"]}


# ---- 0. authorisation, from below ------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["tool-outcomes", "llm-usage", "diagnosis", "actors", "intents"],
)
def test_endpoints_reject_an_unauthenticated_caller(api_client, path):
    r = api_client.get(f"/admin/analytics/{path}")
    assert r.status_code in (401, 403)


@pytest.mark.parametrize(
    "path",
    ["tool-outcomes", "llm-usage", "diagnosis", "actors", "intents"],
)
def test_endpoints_reject_a_non_admin_role(api_client, path):
    """A `viewer` authenticates fine and must still be refused.

    Tested from the bottom of the role ladder on purpose: a suite that only ever
    calls these as an admin proves the happy path and nothing about the gate.
    """

    u = _Admin(role="viewer")
    try:
        r = api_client.get(f"/admin/analytics/{path}", headers=u.headers)
        assert r.status_code == 403
    finally:
        u.drop()


# ---- 1. the filter actually filters ----------------------------------------


def test_lang_filter_narrows_the_result(api_client, seeded, admin):
    """A declared-but-ignored filter answers 200 with everything; this catches it.

    The assertion is not "200 with rows" but "the Burmese-only window contains
    the Burmese turn and NOT the English ones" — the shape a dropped parameter
    cannot produce.
    """

    all_langs = _get(api_client, "intents", admin.headers)
    my_only = _get(api_client, "intents", admin.headers, lang="MY")

    assert all_langs["turns"] == 4
    assert my_only["turns"] == 1                       # only the Burmese turn
    assert my_only["turns"] < all_langs["turns"]


def test_a_bogus_filter_value_narrows_to_nothing(api_client, seeded, admin):
    """The negative control. A filter nobody's data matches must return EMPTY.

    This is the test that fails if a parameter is silently dropped: an ignored
    `lang=zz-nonexistent` returns the full four turns and reads as success
    everywhere else.
    """

    body = _get(api_client, "intents", admin.headers, lang="zz-nonexistent")
    assert body["turns"] == 0
    assert body["buckets"] == []

    tools = _get(api_client, "tool-outcomes", admin.headers, lang="zz-nonexistent")
    assert tools["bars"] == []
    assert tools["totals"]["calls"] == 0

    llm = _get(api_client, "llm-usage", admin.headers, lang="zz-nonexistent")
    assert llm["rows"] == []


@pytest.mark.parametrize(
    "param, value, kept",
    [
        ("path", "cache", 1),          # only the cached turn
        ("cached", "true", 1),
        ("cached", "false", 3),
        ("model", "test/turn-model", 3),   # the cache-path turn carries no model
        ("store", SIBLING, 1),
    ],
)
def test_every_shared_filter_is_wired(api_client, seeded, admin, param, value, kept):
    """Each shared parameter, one at a time, against a known expected count.

    Parametrised rather than asserted in a loop so a single dropped filter names
    itself in the failure output instead of hiding inside one red test.
    """

    body = _get(api_client, "intents", admin.headers, **{param: value})
    assert body["turns"] == kept


def test_a_bare_end_date_includes_that_whole_day(api_client, seeded, admin):
    """§4 as amended: a bare date means THROUGH that day, not up to its midnight.

    `end=<day 3>` therefore keeps turn `d`, which is on day 3. The original
    reading (plain exclusive) dropped it, and a bound that quietly loses the most
    recent day is the error nobody reports — the chart still looks plausible.
    """

    body = api_client.get(
        "/admin/analytics/intents",
        params={"start": WINDOW_START, "end": _D[3]},
        headers=admin.headers,
    ).json()
    assert body["turns"] == 4          # a, b, c AND d

    # One day earlier does drop it, so the bound is real and not just permissive.
    earlier = api_client.get(
        "/admin/analytics/intents",
        params={"start": WINDOW_START, "end": _D[2]},
        headers=admin.headers,
    ).json()
    assert earlier["turns"] == 3


def test_an_end_with_a_time_is_exclusive_at_that_instant(
    api_client, seeded, admin
):
    """The other half of the rule: a timestamp means exactly what it says."""

    body = api_client.get(
        "/admin/analytics/intents",
        params={"start": WINDOW_START, "end": f"{_D[3]}T10:00:00+00:00"},
        headers=admin.headers,
    ).json()
    assert body["turns"] == 3          # d is AT 10:00 on day 3, so excluded

    body = api_client.get(
        "/admin/analytics/intents",
        params={"start": WINDOW_START, "end": f"{_D[3]}T10:00:01+00:00"},
        headers=admin.headers,
    ).json()
    assert body["turns"] == 4


def test_from_to_and_start_end_mean_the_same_thing(api_client, seeded, admin):
    """Both spellings, same value, same answer (§4 amended)."""

    legacy = api_client.get(
        "/admin/analytics/summary",
        params={"from": WINDOW_START, "to": _D[3]}, headers=admin.headers,
    ).json()["turns"]
    contract = api_client.get(
        "/admin/analytics/summary",
        params={"start": WINDOW_START, "end": _D[3]}, headers=admin.headers,
    ).json()["turns"]
    assert legacy == contract == 4


@pytest.mark.parametrize("a, b", [("from", "start"), ("to", "end")])
def test_conflicting_window_spellings_are_a_400(api_client, admin, a, b):
    """A caller sending both with different values gets an error, not a guess.

    Silently preferring one produces a window narrower than the one on screen,
    which is unfalsifiable from outside the server.
    """

    r = api_client.get(
        "/admin/analytics/summary",
        params={a: _D[0], b: _D[2]}, headers=admin.headers,
    )
    assert r.status_code == 400
    assert a in r.json()["detail"] and b in r.json()["detail"]

    # The same value twice is NOT a conflict — the console sends both spellings.
    ok = api_client.get(
        "/admin/analytics/summary",
        params={a: _D[0], b: _D[0]}, headers=admin.headers,
    )
    assert ok.status_code == 200


def test_embed_none_selects_unattributed_turns(api_client, seeded, admin):
    """`embed=none` means IS NULL, not the literal string 'none'."""

    body = _get(api_client, "intents", admin.headers, embed="none")
    assert body["turns"] == 4          # every seeded turn has a NULL embed_id


def test_rated_is_validated_not_silently_ignored(api_client, admin):
    r = api_client.get(
        "/admin/analytics/intents",
        params={"rated": "sideways"},
        headers=admin.headers,
    )
    assert r.status_code == 400


def test_a_malformed_date_names_the_parameter(api_client, admin):
    r = api_client.get(
        "/admin/analytics/intents", params={"start": "yesterday"},
        headers=admin.headers,
    )
    assert r.status_code == 400
    assert "start" in r.json()["detail"]


# ---- 2. store scope: enforced, and `store` can only narrow -----------------


def test_pinned_caller_sees_only_their_own_branch(api_client, seeded, pinned):
    """The sibling's FAILED tool call must be invisible to a branch manager."""

    body = _get(api_client, "tool-outcomes", pinned.headers)
    bars = _bars(body)
    assert set(bars) == {"get_stock", "get_dosage", "find_at_other_stores"}
    # The only get_stock this caller may see is their own succeeded one; the
    # sibling's failure belongs to another branch.
    assert bars["get_stock"]["succeeded"] == 1
    assert bars["get_stock"]["failed"] == 0
    assert body["totals"]["failed"] == 1        # only their own turn `d`


def test_pinned_caller_cannot_widen_scope_with_the_store_param(
    api_client, seeded, pinned
):
    """`?store=<sibling>` must return NOTHING, never the sibling's rows.

    This is the leak this endpoint family already shipped once. If `store`
    replaced the scope instead of being ANDed with it, the boundary would be one
    query parameter away — and the response would look perfectly normal.
    """

    body = _get(api_client, "tool-outcomes", pinned.headers, store=SIBLING)
    assert body["bars"] == []
    assert body["totals"]["calls"] == 0

    llm = _get(api_client, "llm-usage", pinned.headers, store=SIBLING)
    assert llm["rows"] == []
    assert [r["model"] for r in llm["rows"]] == []

    intents = _get(api_client, "intents", pinned.headers, store=SIBLING)
    assert intents["turns"] == 0

    diag = _get(api_client, "diagnosis", pinned.headers, store=SIBLING)
    assert diag["rows"] == []


def test_unscoped_admin_still_sees_both_branches(api_client, seeded, admin):
    """The control: without a pin, the sibling's failure IS visible.

    Without this, `test_pinned_caller_cannot_widen_scope_with_the_store_param`
    would also pass against an endpoint that returns nothing to anybody.
    """

    bars = _bars(_get(api_client, "tool-outcomes", admin.headers))
    assert bars["get_stock"]["succeeded"] == 1
    assert bars["get_stock"]["failed"] == 1


def test_store_filter_narrows_within_scope_for_an_unpinned_caller(
    api_client, seeded, admin
):
    bars = _bars(_get(api_client, "tool-outcomes", admin.headers, store=SIBLING))
    assert bars["get_stock"]["failed"] == 1
    assert bars["get_stock"]["succeeded"] == 0


def test_trace_of_a_sibling_turn_is_404_for_a_pinned_caller(
    api_client, seeded, pinned
):
    """Not 403 and not a redacted body: a pinned caller learns nothing at all
    about a turn outside their branch, including whether it exists."""

    r = api_client.get(
        f"/admin/analytics/trace/{seeded['b']}", headers=pinned.headers
    )
    assert r.status_code == 404

    ok = api_client.get(
        f"/admin/analytics/trace/{seeded['a']}", headers=pinned.headers
    )
    assert ok.status_code == 200


def test_scoped_actors_does_not_report_global_console_events(
    api_client, seeded, pinned
):
    """`app_events` has no branch column, so a pinned caller gets `null` and a
    flag — never a count that quietly spans other branches."""

    body = _get(api_client, "actors", pinned.headers)
    assert body["scope_limited"] is True
    assert all(r["console_events"] is None for r in body["rows"])
    assert all(r["actor"] != f"b-{seeded['tag']}@corp.mm" for r in body["rows"])


# ---- 3. refused is not failed ----------------------------------------------


def test_refused_is_never_counted_as_failed(api_client, seeded, admin):
    """The rule the whole three-state outcome exists for.

    `get_dosage` deliberately declined. It must appear with refused=1, failed=0
    — and, just as importantly, it must NOT be counted as a success either.
    """

    bars = _bars(_get(api_client, "tool-outcomes", admin.headers))

    assert bars["get_dosage"]["refused"] == 1
    assert bars["get_dosage"]["failed"] == 0
    assert bars["get_dosage"]["succeeded"] == 0

    assert bars["find_at_other_stores"]["failed"] == 1
    assert bars["find_at_other_stores"]["refused"] == 0

    totals = _get(api_client, "tool-outcomes", admin.headers)["totals"]
    assert totals == {"succeeded": 1, "refused": 1, "failed": 2, "calls": 4}


def test_a_refusal_is_not_in_the_diagnosis_queue(api_client, seeded, admin):
    """A correct refusal is not a defect and must not fill the triage list."""

    rows = _get(api_client, "diagnosis", admin.headers)["rows"]
    assert seeded["a"] not in [r["turn_id"] for r in rows]


def test_success_rate_carries_its_denominator(api_client, seeded, admin):
    body = _get(api_client, "tool-outcomes", admin.headers)
    assert body["success_rate"] == {"rate": round(1 / 4, 4), "n": 4}

    empty = _get(api_client, "tool-outcomes", admin.headers, lang="zz-nonexistent")
    # No sample -> the rate is UNKNOWN, not 0%. A bare 0.0 renders as a real
    # measurement of a perfect failure.
    assert empty["success_rate"] == {"rate": None, "n": 0}


# ---- 4. null, never zero ---------------------------------------------------


def test_unconfigured_cost_is_null_and_not_zero(api_client, seeded, admin):
    """The `0.0 reads as free` rule (§3), on the endpoint that would show it."""

    rows = {r["model"]: r for r in _get(api_client, "llm-usage", admin.headers)["rows"]}

    a = rows["test/model-a"]
    assert a["cost_usd"] is None            # NOT 0.0
    assert a["cost_is_estimated"] is False
    assert a["priced_calls"] == 0
    # Coverage IS measured here — one call, none of it priced — so 0.0 is the
    # honest rate. It is the cost itself that must stay null.
    assert a["cost_coverage"] == {"rate": 0.0, "n": 1}
    assert a["prompt_tokens"] == 10         # a measured zero would still be 0
    assert a["cache_read_tokens"] == 2
    assert a["cache_creation_tokens"] == 1

    b = rows["test/model-b"]
    assert b["cost_usd"] == 0.5
    assert b["cost_is_estimated"] is True   # derived is flagged as derived
    assert b["cost_coverage"] == {"rate": 1.0, "n": 1}


def test_totals_cost_is_null_when_nothing_is_priced(api_client, seeded, pinned):
    """A pinned caller sees only the unpriced model, so the total is UNKNOWN."""

    totals = _get(api_client, "llm-usage", pinned.headers)["totals"]
    assert totals["cost_usd"] is None
    assert totals["calls"] == 1


def test_p50_ttft_is_null_when_never_measured(api_client, seeded, admin):
    """A model with no ttft recorded reports null, not 0ms."""

    _ensure_schema()
    turn = _turn(ts=f"{_D[0]} 11:00:00+00", store=MINE,
                 question="no ttft here", answer="x")
    _llm(turn, 0, "test/model-c", prompt=1, ttft=None)
    try:
        rows = {r["model"]: r
                for r in _get(api_client, "llm-usage", admin.headers)["rows"]}
        assert rows["test/model-c"]["p50_ttft_ms"] is None
        assert rows["test/model-a"]["p50_ttft_ms"] == 300
    finally:
        _pg("DELETE FROM chat_logs WHERE id=$1", turn)


def test_pre_instrumentation_turns_are_not_recorded_not_zero(
    api_client, seeded, admin
):
    """Turn `c` ran no tool and no model call. It is `not_recorded` (§7) — it is
    not a turn that used zero tools, and it must not be in any bucket of the bar
    chart."""

    tools = _get(api_client, "tool-outcomes", admin.headers)
    assert tools["not_recorded"] == 1            # turn `c` only
    assert tools["totals"]["calls"] == 4         # the uninstrumented turn is not here

    llm = _get(api_client, "llm-usage", admin.headers)
    assert llm["not_recorded"] == 2              # `c` and `d` made no llm_call

    intents = _get(api_client, "intents", admin.headers)
    assert intents["turns"] == 4                 # it IS counted as a question
    assert intents["not_recorded"] == 1          # and NOT as a tool it never ran


# ---- 5. diagnosis ----------------------------------------------------------


def test_diagnosis_marks_the_two_signal_turn_as_both(api_client, seeded, admin):
    """Turn `d` failed a tool AND gave up. `both` is the highest-value queue."""

    body = _get(api_client, "diagnosis", admin.headers)
    rows = {r["turn_id"]: r for r in body["rows"]}

    assert rows[seeded["d"]]["issue_type"] == "both"
    assert sorted(rows[seeded["d"]]["signals"]) == ["failed_tool", "gave_up"]
    assert rows[seeded["d"]]["failed_tool_name"] == "find_at_other_stores"
    assert rows[seeded["d"]]["failed_tool_error"] == "timeout"

    assert rows[seeded["b"]]["issue_type"] == "failed_tool"
    assert body["counts"]["both"] == 1
    assert body["counts"]["failed_tool"] == 1


def test_diagnosis_issue_filter_is_applied_before_the_limit(
    api_client, seeded, admin
):
    """`issue=both` returns the both-row even with a limit of 1.

    Filtering the page after slicing it would answer "the newest row is not a
    `both`, so there are none" — an empty queue that is a lie.
    """

    body = _get(api_client, "diagnosis", admin.headers, issue="both", limit=1)
    assert [r["turn_id"] for r in body["rows"]] == [seeded["d"]]
    # The counts are over the whole window, not the page, so a KPI above the
    # list never disagrees with the list.
    assert body["counts"]["failed_tool"] == 1


def test_diagnosis_rate_carries_its_denominator(api_client, seeded, admin):
    body = _get(api_client, "diagnosis", admin.headers)
    assert body["problem_rate"]["n"] == 4        # turns in the window
    assert body["problem_rate"]["rate"] == round(2 / 4, 4)


def test_diagnosis_rejects_an_unknown_issue(api_client, admin):
    r = api_client.get(
        "/admin/analytics/diagnosis", params={"issue": "vibes"},
        headers=admin.headers,
    )
    assert r.status_code == 400


# ---- 6. trace --------------------------------------------------------------


def test_trace_interleaves_tool_and_llm_calls_in_seq_order(
    api_client, seeded, admin
):
    r = api_client.get(f"/admin/analytics/trace/{seeded['a']}", headers=admin.headers)
    assert r.status_code == 200
    body = r.json()

    assert body["turn"]["id"] == seeded["a"]
    assert body["instrumented"] is True
    assert body["tool_calls"] == 2 and body["llm_calls"] == 1
    # seq 0 tool, seq 0 llm (tool first — its result is what the model consumes),
    # then seq 1 tool.
    assert [(c["kind"], c["seq"]) for c in body["calls"]] == [
        ("tool", 0), ("llm", 0), ("tool", 1)
    ]
    assert body["calls"][0]["outcome"] == "succeeded"
    assert body["calls"][2]["outcome"] == "refused"


def test_trace_of_an_uninstrumented_turn_says_so(api_client, seeded, admin):
    """The 122 pre-instrumentation turns: the turn is real, the trace is empty,
    and the payload says which."""

    body = api_client.get(
        f"/admin/analytics/trace/{seeded['c']}", headers=admin.headers
    ).json()
    assert body["calls"] == []
    assert body["instrumented"] is False
    assert body["turn"]["id"] == seeded["c"]


def test_trace_of_an_unknown_turn_is_404(api_client, admin):
    r = api_client.get("/admin/analytics/trace/999999999", headers=admin.headers)
    assert r.status_code == 404


# ---- 7. section isolation / shape stability --------------------------------


@pytest.mark.parametrize(
    "path, keys",
    [
        ("tool-outcomes", {"bars", "totals", "success_rate", "not_recorded",
                           "available"}),
        ("llm-usage", {"rows", "totals", "not_recorded", "available"}),
        ("diagnosis", {"rows", "counts", "turns", "problem_rate", "available"}),
        ("actors", {"rows", "scope_limited", "available"}),
        ("intents", {"buckets", "matrix", "turns", "unclassified",
                     "not_recorded", "available"}),
    ],
)
def test_empty_result_has_the_same_keys_as_a_full_one(
    api_client, seeded, admin, path, keys
):
    """§6: the empty shape is shaped like the real one, so the frontend never
    branches on a missing key."""

    full = _get(api_client, path, admin.headers)
    empty = _get(api_client, path, admin.headers, lang="zz-nonexistent")
    assert set(full) == keys
    assert set(empty) == keys


def test_actor_rows_have_a_stable_key_set(api_client, seeded, admin):
    """Both halves of `/actors` (turns and console events) build rows through
    one factory, so no column exists on only some rows."""

    rows = _get(api_client, "actors", admin.headers)["rows"]
    assert rows
    assert len({frozenset(r) for r in rows}) == 1


def test_intents_buckets_include_the_burmese_terms(api_client, seeded, admin):
    """Roughly half this product's traffic is Burmese. An English-only bucket
    list would file `ဆေး ရှိလား` under `other` and the page would report that
    nobody asks about stock."""

    body = _get(api_client, "intents", admin.headers, lang="MY")
    assert [b["bucket"] for b in body["buckets"]] == ["stock"]
    assert body["unclassified"] == 0


def test_intents_matrix_pairs_intent_with_tool(api_client, seeded, admin):
    matrix = _get(api_client, "intents", admin.headers)["matrix"]
    cells = {(c["intent"], c["tool"]): c["n"] for c in matrix["cells"]}
    assert cells[("stock", "get_stock")] == 1
    assert cells[("stock", "get_dosage")] == 1
    assert "get_stock" in matrix["tools"]


def test_intent_shares_carry_their_denominator(api_client, seeded, admin):
    body = _get(api_client, "intents", admin.headers)
    for bucket in body["buckets"]:
        assert bucket["share"]["n"] == body["turns"]
        assert 0 <= bucket["share"]["rate"] <= 1


# ---- 8. the EXISTING endpoints now take the same filter object -------------
#
# Contract §4 says "every analytics endpoint takes the same object", and the
# console emits all ten on every request. Before this, `model`, `actor`, `cached`
# and `rated` were declared on none of the older endpoints, so they were dropped
# silently and those pages answered unfiltered underneath their own filter chips.
# The tests below are per-parameter and per-endpoint for the same reason the new
# ones are: a dropped filter must name itself.

# The endpoints that read `chat_logs` and can be exercised with the base fixture.
#
# `repeats` and `cost` are NOT here, and the omission is deliberate rather than
# convenient: `repeats` only returns questions asked more than once, and `cost`
# reports `available: false` until some turn carries a token count. Against the
# base fixture both answer "nothing" to every caller, so a narrowing assertion
# over them could not fail and would be a test that proves the filter works by
# proving nothing works. They get their own test below, with the data they need.
_LEGACY_ENDPOINTS = ("summary", "questions", "embeds", "timeseries",
                     "tools", "paths")


def _legacy_turn_count(client, endpoint, headers, **params):
    """How many seeded turns an older endpoint says it matched.

    Each of these returns a different shape, so the count has to be read out of
    whichever field carries it. Asserting on `200 OK` instead would pass against
    an endpoint that ignores the filter completely — the exact failure §4 warns
    about — so every endpoint here is reduced to a NUMBER that has to move.
    """

    body = _get(client, endpoint, headers, **params)
    if endpoint == "summary":
        return body["turns"]
    if endpoint == "questions":
        return body["total"]
    if endpoint in ("embeds", "paths"):
        return sum(r["turns"] for r in body)
    if endpoint == "repeats":
        # `asked`, not `turns`: this endpoint groups by question text and counts
        # how often each was asked. Reading a key it does not have would make
        # every assertion here 0 < 0 — a silently vacuous test.
        return sum(r["asked"] for r in body)
    if endpoint == "timeseries":
        return sum(r["turns"] for r in body["rows"])
    if endpoint == "tools":
        return body["total_turns_with_tools"] + body["not_recorded"]
    if endpoint == "cost":
        return sum(r["turns"] for r in body["rows"]) if body["available"] else 0
    raise AssertionError(f"unmapped endpoint {endpoint}")


@pytest.mark.parametrize("endpoint", _LEGACY_ENDPOINTS)
@pytest.mark.parametrize("param, value", [
    ("model", "test/turn-model"),
    ("cached", "true"),
    ("rated", "down"),
    ("actor", "none"),
])
def test_legacy_endpoints_declare_the_new_shared_filters(
    api_client, seeded, admin, endpoint, param, value
):
    """Each new §4 filter must NARROW each existing endpoint.

    The comparison is against the same endpoint unfiltered, so this cannot pass
    by the endpoint returning nothing to everyone. `cost` is exempt from the
    strict inequality only when it reports no token data at all — it then has no
    rows to narrow, and says so through `available: false`.
    """

    unfiltered = _legacy_turn_count(api_client, endpoint, admin.headers)
    filtered = _legacy_turn_count(api_client, endpoint, admin.headers,
                                  **{param: value})
    assert unfiltered > 0, f"{endpoint} sees no seeded turns at all"
    assert filtered < unfiltered, (
        f"{endpoint} ignored ?{param}={value} — {filtered} == {unfiltered}"
    )


@pytest.mark.parametrize("endpoint", _LEGACY_ENDPOINTS)
def test_legacy_endpoints_honour_the_contract_window(
    api_client, seeded, admin, endpoint
):
    """`start`/`end` are the contract's spelling and must work on their own.

    `_get` already sends them on every call, so this asserts the other half: a
    window that excludes the last day drops that day's turn.
    """

    full = _legacy_turn_count(api_client, endpoint, admin.headers)
    short = _legacy_turn_count(api_client, endpoint, admin.headers, end=_D[2])
    assert short < full


@pytest.mark.parametrize("endpoint", _LEGACY_ENDPOINTS)
def test_legacy_endpoints_reject_a_bad_rated_value(api_client, admin, endpoint):
    """Validation lives on the shared dependency, so it reaches every endpoint."""

    r = api_client.get(
        f"/admin/analytics/{endpoint}", params={"rated": "sideways"},
        headers=admin.headers,
    )
    assert r.status_code == 400


def test_legacy_store_filter_still_cannot_widen_a_pinned_scope(
    api_client, seeded, pinned
):
    """The retrofit must not have loosened the boundary on the old endpoints.

    `store` became comma-capable in the same change; a pinned caller passing the
    sibling — alone or in a list including their own branch — still gets only
    their own turns, because the scope predicate is ANDed, not replaced.
    """

    assert _legacy_turn_count(api_client, "summary", pinned.headers,
                              store=SIBLING) == 0
    both = _legacy_turn_count(api_client, "summary", pinned.headers,
                              store=f"{MINE},{SIBLING}")
    assert both == 3          # a, c, d — the sibling's turn is still invisible


# ---- 9. `embed=none` reaches the unattributed turns ------------------------


def test_embed_none_selects_null_embed_ids_on_the_legacy_endpoints(
    api_client, seeded, admin
):
    """§4 reserves `none` for unattributed turns, and it used to return ZERO.

    `_log_filters` matched `embed_id = 'none'` exactly, so the Embeds tab's
    "Unattributed" drill-through led to a page reporting that there are none —
    over the ~122 turns that really are unattributed. An empty result reads as a
    measurement, which is worse than a dropped parameter. Reported by the UI
    agent; this pins the IS NULL branch.
    """

    # All four seeded turns have a NULL embed_id.
    assert _legacy_turn_count(api_client, "summary", admin.headers,
                              embed="none") == 4

    # And it is IS NULL, not the literal string: a turn whose embed_id really is
    # the four characters "none" is a different row and must not be swept in.
    turn = _turn(ts=f"{_D[0]} 12:00:00+00", store=MINE, embed="none",
                 question="literal none embed", answer="x")
    try:
        assert _legacy_turn_count(api_client, "summary", admin.headers,
                                  embed="none") == 4      # not 5
        assert _legacy_turn_count(api_client, "summary", admin.headers) == 5
    finally:
        _pg("DELETE FROM chat_logs WHERE id=$1", turn)


def test_embed_none_works_on_the_new_endpoints_too(api_client, seeded, admin):
    assert _get(api_client, "intents", admin.headers, embed="none")["turns"] == 4


def test_comma_separated_filters_are_a_union(api_client, seeded, admin):
    """§4 spells these as lists. A single value behaves exactly as before."""

    assert _legacy_turn_count(api_client, "summary", admin.headers,
                              lang="EN") == 3
    assert _legacy_turn_count(api_client, "summary", admin.headers,
                              lang="MY") == 1
    assert _legacy_turn_count(api_client, "summary", admin.headers,
                              lang="EN,MY") == 4


# ---- 10. log_chat hands back the turn id ----------------------------------


def test_log_chat_returns_the_new_turn_id():
    """`tool_calls.turn_id` is NOT NULL REFERENCES chat_logs(id), so the id is
    the seam between a logged turn and its instrumentation. It used to return
    None and nothing downstream could learn it."""

    from app.admin import log_chat
    from app.db import close_pool

    async def go():
        try:
            return await log_chat(
                "id round trip", "answer", MINE, False, 12,
                model="test/turn-model", path="agent",
            )
        finally:
            await close_pool()

    turn_id = asyncio.run(go())
    try:
        assert isinstance(turn_id, int)
        rows = _pg("SELECT question FROM chat_logs WHERE id=$1", turn_id,
                   fetch=True)
        assert rows[0]["question"] == "id round trip"

        # And the id is usable as a foreign key, which is the entire point.
        _ensure_schema()
        _tool(turn_id, 0, "get_stock", "succeeded", duration=5)
    finally:
        _pg("DELETE FROM chat_logs WHERE id=$1", turn_id)


def test_log_chat_still_never_raises_when_the_write_fails():
    """The id must not have turned a best-effort logger into a required one.

    A logging failure returning None is a gap in the audit trail; a logging
    failure RAISING is a pharmacy that got no answer. That ordering is the
    docstring's, and it is not negotiable.
    """

    from unittest import mock

    from app import admin as adminmod

    async def go():
        with mock.patch.object(adminmod, "q", side_effect=RuntimeError("db down")):
            return await adminmod.log_chat("q", "a", MINE, False, 1)

    assert asyncio.run(go()) is None


@pytest.fixture
def repeatable():
    """Two identical questions plus token/cost figures, in the same window.

    `repeats` needs a question asked twice before it returns any row at all, and
    `cost` needs a turn carrying tokens before it reports `available: true`.
    Without both, neither endpoint can demonstrate that a filter narrowed it.
    """

    _ensure_schema()
    ids = []
    for i, cached in enumerate((False, False)):
        ids.append(_turn(ts=f"{_D[i]} 09:00:00+00", store=MINE,
                         question="repeated question", answer="same answer",
                         cached=cached))
    # The third differs in model AND actor, so `?model=` / `?actor=none` have
    # something to narrow AWAY. Three identical rows would satisfy every filter
    # equally and the assertion `filtered < unfiltered` could never fail.
    ids.append(_turn(ts=f"{_D[2]} 09:00:00+00", store=MINE, cached=True,
                     model="other/model", actor="someone@corp.mm", role="admin",
                     question="repeated question", answer="same answer"))
    _pg("UPDATE chat_logs SET input_tokens=10, output_tokens=5, total_tokens=15,"
        "                     cost_usd='0.002'::numeric"
        " WHERE id = ANY($1::bigint[])", ids)
    yield ids
    _pg("DELETE FROM chat_logs WHERE id = ANY($1::bigint[])", ids)


@pytest.mark.parametrize("endpoint", ["repeats", "cost"])
@pytest.mark.parametrize("param, value", [
    ("model", "test/turn-model"),
    ("cached", "true"),
    ("actor", "none"),
])
def test_repeats_and_cost_declare_the_new_shared_filters(
    api_client, seeded, repeatable, admin, endpoint, param, value
):
    unfiltered = _legacy_turn_count(api_client, endpoint, admin.headers)
    filtered = _legacy_turn_count(api_client, endpoint, admin.headers,
                                  **{param: value})
    assert unfiltered > 0, f"{endpoint} sees no seeded turns at all"
    assert filtered < unfiltered, (
        f"{endpoint} ignored ?{param}={value} — {filtered} == {unfiltered}"
    )


@pytest.mark.parametrize("endpoint", ["repeats", "cost"])
def test_repeats_and_cost_honour_the_contract_window(
    api_client, seeded, repeatable, admin, endpoint
):
    full = _legacy_turn_count(api_client, endpoint, admin.headers)
    short = _legacy_turn_count(api_client, endpoint, admin.headers, end=_D[1])
    assert short < full


def test_rated_filter_is_not_trivially_true(api_client, seeded, admin):
    """`?rated=down` must return the RATED turns, not the whole table.

    `chat_feedback` has its own `question`/`answer` columns, so an unqualified
    correlated reference inside the EXISTS subquery resolves both sides to
    `chat_feedback` — a predicate true for every feedback row, which returns
    everything under a filter chip that says "rated down". It shipped that way
    for the length of one test run and this is the guard.
    """

    fb = _pg(
        '''INSERT INTO chat_feedback (turn_id, question, answer, verdict, store_id)
           VALUES ($1,$2,$3,'down',$4) RETURNING id''',
        seeded["c"], "ဆေး ရှိလား", "ရှိပါတယ်", MINE, fetch=True,
    )[0]["id"]
    try:
        down = _legacy_turn_count(api_client, "summary", admin.headers,
                                  rated="down")
        up = _legacy_turn_count(api_client, "summary", admin.headers, rated="up")
        both = _legacy_turn_count(api_client, "summary", admin.headers,
                                  rated="any")
        assert down == 1        # the Burmese turn, and ONLY it
        assert up == 0
        assert both == 1
    finally:
        _pg("DELETE FROM chat_feedback WHERE id=$1", fb)


def test_rated_filter_returns_nothing_when_nothing_is_rated(
    api_client, seeded, admin
):
    """The negative control for the test above: with no feedback rows at all,
    `rated=down` must be empty rather than everything."""

    assert _legacy_turn_count(api_client, "summary", admin.headers,
                              rated="down") == 0


# ---- 11. the store filter is ANCHORED, not a substring ---------------------


def test_store_filter_does_not_substring_match_a_sibling_branch(
    api_client, seeded, admin
):
    """`store=20005` must not also return 200059-CCZZ.

    This was `store_id ILIKE '%'||$n||'%'`, so on a real chain `store=CMHL-1`
    returned CMHL-10, CMHL-19 and CMHL-100 as well — a branch manager's own
    filter quietly showing them three other branches' turns. It is the same
    substring trap `_site_clause` exists to prevent, and CLAUDE.md records it
    shipping once already on the enforced scope.

    The decoy is the technique from test_admin_scope: a store code that CONTAINS
    the token but is a different branch. Anchored matching excludes it; the old
    ILIKE returned it.
    """

    decoy = _turn(ts=f"{_D[0]} 13:00:00+00", store="200059-CCZZ",
                  question="decoy branch turn", answer="x")
    try:
        # Unfiltered, both are in the window — so the filter has something to
        # get wrong. Without this the assertion below could not fail.
        assert _legacy_turn_count(api_client, "summary", admin.headers) == 5

        narrowed = _legacy_turn_count(api_client, "summary", admin.headers,
                                      store="20005")
        assert narrowed == 3          # a, c, d — NOT the 200059-CCZZ decoy
    finally:
        _pg("DELETE FROM chat_logs WHERE id=$1", decoy)


@pytest.mark.parametrize("token", ["20005-CCYK", "20005", "CCYK"])
def test_store_filter_accepts_every_site_token_form(
    api_client, seeded, admin, token
):
    """Full code, numeric prefix and alpha suffix all address the same branch.

    Deliberately NOT plain `=`. `users.store_id` — the pin that drives
    `caller_store_scope` — accepts all three forms, and a filter that accepted
    only the full code while the scope pin accepted all three would be its own
    trap: the same string would mean two different things depending on where it
    was typed.
    """

    assert _legacy_turn_count(api_client, "summary", admin.headers,
                              store=token) == 3


@pytest.mark.parametrize("partial", ["2000", "CC"])
def test_store_filter_rejects_a_partial_token(api_client, seeded, admin, partial):
    """A partial token matches NOTHING, not everything.

    Every real site code starts `2000` and contains `CC`, so a substring match
    returns the whole estate here. Zero is also the right direction to fail.
    """

    assert _legacy_turn_count(api_client, "summary", admin.headers,
                              store=partial) == 0


def test_store_filter_list_is_a_union(api_client, seeded, admin):
    assert _legacy_turn_count(api_client, "summary", admin.headers,
                              store=MINE) == 3
    assert _legacy_turn_count(api_client, "summary", admin.headers,
                              store=SIBLING) == 1
    assert _legacy_turn_count(api_client, "summary", admin.headers,
                              store=f"{MINE},{SIBLING}") == 4


def test_embed_list_mixes_the_none_sentinel_with_real_ids(
    api_client, seeded, admin
):
    """`embed=none,livetest` = unattributed OR livetest, per the contract."""

    attributed = _turn(ts=f"{_D[0]} 14:00:00+00", store=MINE, embed="livetest",
                       question="an attributed turn", answer="x")
    try:
        assert _legacy_turn_count(api_client, "summary", admin.headers,
                                  embed="none") == 4
        assert _legacy_turn_count(api_client, "summary", admin.headers,
                                  embed="livetest") == 1
        assert _legacy_turn_count(api_client, "summary", admin.headers,
                                  embed="none,livetest") == 5
    finally:
        _pg("DELETE FROM chat_logs WHERE id=$1", attributed)


def test_lang_list_folds_case(api_client, seeded, admin):
    """`lang=en` and `lang=EN` are the same language; there is no third meaning."""

    assert _legacy_turn_count(api_client, "summary", admin.headers,
                              lang="en") == 3
    assert _legacy_turn_count(api_client, "summary", admin.headers,
                              lang="en,my") == 4


# ---- 12. rated joins on turn_id, and never guesses from text ---------------


def test_rated_does_not_match_a_different_turn_with_the_same_words(
    api_client, seeded, admin
):
    """Two turns with identical text are two turns, and one rating is one rating.

    The text-matching join counted a single thumbs-down against every turn that
    happened to use the same words — a real risk here, where "ဆေး ရှိလား" is
    close to the most common question in the corpus. The FK cannot do that.
    """

    twin = _turn(ts=f"{_D[0]} 15:00:00+00", store=MINE, lang="MY",
                 question="ဆေး ရှိလား", answer="ရှိပါတယ်")
    fb = _pg(
        """INSERT INTO chat_feedback (turn_id, question, answer, verdict, store_id)
           VALUES ($1,$2,$3,'down',$4) RETURNING id""",
        seeded["c"], "ဆေး ရှိလား", "ရှိပါတယ်", MINE, fetch=True,
    )[0]["id"]
    try:
        rated = _legacy_turn_count(api_client, "summary", admin.headers,
                                   rated="down")
        assert rated == 1          # the rated turn only — not its twin
    finally:
        _pg("DELETE FROM chat_feedback WHERE id=$1", fb)
        _pg("DELETE FROM chat_logs WHERE id=$1", twin)


def test_a_rating_with_no_turn_id_is_unattributed_not_guessed(
    api_client, seeded, admin
):
    """Pre-FK rows keep NULL and match nothing. Unattributable, and said so."""

    fb = _pg(
        """INSERT INTO chat_feedback (question, answer, verdict, store_id)
           VALUES ($1,$2,'down',$3) RETURNING id""",
        "ဆေး ရှိလား", "ရှိပါတယ်", MINE, fetch=True,
    )[0]["id"]
    try:
        assert _legacy_turn_count(api_client, "summary", admin.headers,
                                  rated="down") == 0
    finally:
        _pg("DELETE FROM chat_feedback WHERE id=$1", fb)


def test_post_feedback_stores_the_turn_id(api_client, seeded, admin):
    """The write path that makes the FK join possible at all."""

    r = api_client.post(
        "/admin/feedback",
        json={"turn_id": seeded["a"], "question": "q", "answer": "a",
              "verdict": "down"},
        headers=admin.headers,
    )
    assert r.status_code == 200, r.text
    fb_id = r.json()["id"]
    try:
        rows = _pg("SELECT turn_id FROM chat_feedback WHERE id=$1", fb_id,
                   fetch=True)
        assert rows[0]["turn_id"] == seeded["a"]
        assert _legacy_turn_count(api_client, "summary", admin.headers,
                                  rated="down") == 1
    finally:
        _pg("DELETE FROM chat_feedback WHERE id=$1", fb_id)


# ---- 13. a block that cannot obey the filters says so ----------------------


def test_feedback_block_declares_the_filters_it_cannot_honour(
    api_client, seeded, admin
):
    """§5 amended: `filters_applied: false` when a chip cannot reach the number.

    `chat_feedback` has no lang/embed/path/actor/cached column, so those filters
    cannot narrow the feedback KPI. The payload says which were ignored; the UI
    marks the number unfiltered. Permanent, not a stopgap.
    """

    clean = _get(api_client, "summary", admin.headers)["feedback"]
    assert clean["filters_applied"] is True
    assert clean["ignored_filters"] == []

    blind = _get(api_client, "summary", admin.headers, lang="MY")["feedback"]
    assert blind["filters_applied"] is False
    assert blind["ignored_filters"] == ["lang"]

    many = _get(api_client, "summary", admin.headers,
                lang="MY", path="cache", cached="true")["feedback"]
    assert many["filters_applied"] is False
    assert set(many["ignored_filters"]) == {"lang", "path", "cached"}

    # `store` and the window ARE honoured, so they must NOT be listed.
    scoped = _get(api_client, "summary", admin.headers, store=MINE)["feedback"]
    assert scoped["filters_applied"] is True


# ---- 14. the two series the UI asked for -----------------------------------


def test_timeseries_reports_feedback_per_bucket(api_client, seeded, admin):
    """`up`/`down` per day, joined on the FK (approved addition).

    Counted as RATINGS, not as rated turns: two people disliking the same answer
    is two pieces of feedback, and a per-turn boolean would report it as one.
    """

    fbs = [
        _pg("""INSERT INTO chat_feedback (turn_id, question, answer, verdict)
               VALUES ($1,'q','a',$2) RETURNING id""",
            seeded["a"], verdict, fetch=True)[0]["id"]
        for verdict in ("down", "down", "up")
    ]
    try:
        body = _get(api_client, "timeseries", admin.headers)
        assert body["feedback_available"] is True
        by_day = {r["t"]: r for r in body["rows"]}

        assert by_day[_D[0]]["down"] == 2      # two ratings, not one rated turn
        assert by_day[_D[0]]["up"] == 1
        # A day with traffic and no ratings is a MEASURED zero — we can see the
        # feedback table, so 0 is a fact rather than an absence.
        assert by_day[_D[1]]["down"] == 0
        assert by_day[_D[1]]["up"] == 0
    finally:
        for fb in fbs:
            _pg("DELETE FROM chat_feedback WHERE id=$1", fb)


def test_timeseries_feedback_obeys_the_shared_filters(api_client, seeded, admin):
    """The new series is filtered like every other column on the row.

    A series that ignored the filter bar would put ratings from turns the chart
    is not showing onto the chart that is showing.
    """

    fb = _pg(
        """INSERT INTO chat_feedback (turn_id, question, answer, verdict)
           VALUES ($1,'q','a','down') RETURNING id""",
        seeded["a"], fetch=True,
    )[0]["id"]
    try:
        # Turn `a` is EN; filtering to Burmese must drop its rating too.
        rows = _get(api_client, "timeseries", admin.headers, lang="MY")["rows"]
        assert sum(r["down"] for r in rows) == 0

        rows = _get(api_client, "timeseries", admin.headers, lang="EN")["rows"]
        assert sum(r["down"] for r in rows) == 1
    finally:
        _pg("DELETE FROM chat_feedback WHERE id=$1", fb)


def test_timeseries_traffic_survives_an_unreadable_feedback_table(
    api_client, seeded, admin
):
    """A missing ratings column must not cost the operator their traffic chart.

    The feedback series is its own query for exactly this reason. Simulated by
    pointing the join at a column that does not exist — the same shape as a
    database that has not picked up `chat_feedback.turn_id`.
    """

    from unittest import mock

    from app import admin as adminmod

    real_q = adminmod.q

    async def flaky(sql, *args):
        if "chat_feedback cf" in sql:
            raise RuntimeError("no such column: turn_id")
        return await real_q(sql, *args)

    with mock.patch.object(adminmod, "q", flaky):
        body = _get(api_client, "timeseries", admin.headers)

    assert body["feedback_available"] is False
    assert sum(r["turns"] for r in body["rows"]) == 4     # traffic intact
    # null, not 0 — "we cannot see" is not "nobody rated it".
    assert all(r["up"] is None and r["down"] is None for r in body["rows"])


def test_data_health_reports_ingest_by_day(api_client, admin):
    """`by_day` on data-health (approved addition), from `ingest_events`.

    A table tells you how many rows it holds now, never how many arrived on
    Tuesday or how many files were turned away — so this reads the event log.
    """

    import uuid as _uuid

    run = str(_uuid.uuid4())
    _pg("""INSERT INTO ingest_events (run_id, file, step, status, data, at)
           VALUES ($1,'probe.xlsx','loaded','ok','{"rows": 42}'::jsonb,
                   $2::text::timestamptz)""", run, f"{_D[0]} 08:00:00+00")
    _pg("""INSERT INTO ingest_events (run_id, file, step, status, at)
           VALUES ($1,'bad.xlsx','rejected','bad', $2::text::timestamptz)""",
        run, f"{_D[0]} 09:00:00+00")
    try:
        body = api_client.get("/admin/analytics/data-health",
                              headers=admin.headers).json()
        assert body["by_day"] is not None
        day = {r["day"]: r for r in body["by_day"]}[_D[0]]
        assert day["rows"] == 42
        assert day["rejected"] == 1
        assert day["files"] == 2
    finally:
        _pg("DELETE FROM ingest_events WHERE run_id=$1::uuid", run)


def test_data_health_by_day_is_null_when_it_cannot_be_read(api_client, admin):
    """`null`, not `[]`. An empty list says "no ingests happened"; null says
    "we cannot see", and only one of those should send somebody to look."""

    from unittest import mock

    from app import admin as adminmod

    real_q = adminmod.q

    async def flaky(sql, *args):
        if "FROM ingest_events" in sql and "step = 'loaded'" in sql:
            raise RuntimeError("relation does not exist")
        return await real_q(sql, *args)

    with mock.patch.object(adminmod, "q", flaky):
        body = api_client.get("/admin/analytics/data-health",
                              headers=admin.headers).json()

    assert body["by_day"] is None
    assert body["catalog"] is not None          # the rest of the panel survives
