"""Analytics / audit / diagnostics surface: aggregates, filters, and the store pin.

Three things are being pinned here, in descending order of how bad it is to get
them wrong:

1. **A branch-pinned admin must not read another branch's questions.** These
   endpoints return customers' questions and the agent's answers verbatim — the
   most sensitive rows in this system, well past the stock figures that
   `catalog_one` / `inventory` / `stores` were already fixed for. The scope test
   uses the decoy technique from ``test_admin_scope`` / ``test_tools_scope``: a
   site code that CONTAINS the scope token but is a different branch
   (``200059-CCZZ`` contains ``20005``), so a substring matcher fails the test
   and an anchored one passes. Without the decoy the test cannot tell the two
   apart, which is the vacuous-guard trap CLAUDE.md records.

2. **The numbers are true.** Percentiles, buckets, distinct/repeat rate and
   cache rate are asserted against rows this file seeded, not against whatever
   the dev database happens to hold.

3. **Logging never breaks an answer.** ``log_chat`` is on the answer path; a
   database blip there must cost a log row, never a reply.

Needs live Postgres, like the rest of the suite.

⚠️ Every DB setup/teardown goes through :func:`_pg`, a throwaway asyncpg
connection — NOT ``app.db.q``. The shared pool is bound to whichever loop created
it, and under ``api_client`` that is the TestClient's portal loop; an
``asyncio.run`` in a test body would hand that pool to a second loop and raise
"attached to a different loop".
"""

from __future__ import annotations

import asyncio
import uuid
from unittest import mock

import pytest

from app import auth as authmod
from app.config import Settings, get_settings

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
    """chat_logs with the audit columns, without importing the app's pool.

    Mirrors ``app.admin.ensure_chat_logs`` exactly — including the ALTERs, which
    are the whole point: a database created before the audit columns existed
    would otherwise still be on the six-column shape, and every test below would
    fail on a missing column rather than on a real defect.
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
    ):
        _pg(f"ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS {col} {typ}")
    _pg("CREATE INDEX IF NOT EXISTS idx_chat_logs_ts ON chat_logs (ts DESC)")
    _pg("CREATE INDEX IF NOT EXISTS idx_chat_logs_embed_ts ON chat_logs (embed_id, ts DESC)")


# ---- seeding ---------------------------------------------------------------

# Every seeded row carries this marker in its question, so the fixture can find
# and delete exactly its own rows and every query below can narrow to them with
# `q=` — the dev database has 122 real turns in it and an aggregate over
# "whatever is in the table" is a test that asserts nothing.
MARK = f"zzmark{uuid.uuid4().hex[:10]}"

MINE = "20005-CCYK"
SIBLING = "20024-CC73"
# Contain a scope token but are DIFFERENT branches: `200059-CCZZ` contains
# `20005`, `20099-CCYKX` contains `CCYK`. Anchored matching (_site_clause)
# excludes both; the documented-bad `ILIKE '%'||$n||'%'` returns them. One decoy
# per token form, or the parametrisation that has no decoy cannot fail.
DECOY = "200059-CCZZ"
DECOY_SUFFIX = "20099-CCYKX"
DECOYS = (DECOY, DECOY_SUFFIX)


def _insert(**kw):
    _pg(
        """INSERT INTO chat_logs
               (ts, lang, store_id, question, answer, cached, latency_ms,
                embed_id, session_id, model, tools, path)
           VALUES ($1::text::timestamptz,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12)""",
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
    )


@pytest.fixture
def seeded_logs():
    """Ten turns with known latencies, languages, stores, embeds and dates.

    Latencies (ms) for the eight rows at MINE: 3, 50, 4000, 6000, 9000, 12000,
    21000, 30000. Chosen so every bucket boundary in `_BUCKETS` is exercised and
    so p50/p95 are unambiguous.
    """

    _ensure_schema()
    _purge()
    rows = [
        # ts,             lang, latency, cached, embed,   path
        ("2026-08-10T09:00:00Z", "EN", 3, True, "embA", "cache"),
        ("2026-08-10T09:01:00Z", "EN", 50, True, "embA", "cache"),
        ("2026-08-10T09:02:00Z", "EN", 4000, False, "embA", "fast_path"),
        ("2026-08-11T09:03:00Z", "EN", 6000, False, "embA", "agent"),
        ("2026-08-11T09:04:00Z", "MY", 9000, False, "embB", "agent"),
        ("2026-08-11T09:05:00Z", "MY", 12000, False, "embB", "agent"),
        ("2026-08-12T09:06:00Z", "EN", 21000, False, None, "agent"),
        ("2026-08-12T09:07:00Z", "EN", 30000, False, None, "agent"),
    ]
    for i, (ts, lang, ms, cached, embed, path) in enumerate(rows):
        # Two rows share a question text so `distinct` < `turns` and the repeats
        # endpoint has something to group.
        question = f"{MARK} do you have paracetamol" if i < 2 else f"{MARK} question {i}"
        _insert(
            ts=ts, lang=lang, store_id=MINE, question=question,
            answer=f"answer body {i} lidocaine" if i == 3 else f"answer body {i}",
            cached=cached, latency_ms=ms, embed_id=embed,
            session_id=f"sess{i}", model="google/gemini-3.5-flash",
            tools='["get_stock"]', path=path,
        )
    # One turn at a SIBLING branch and one at the substring DECOY. Both must be
    # invisible to an admin pinned to MINE.
    _insert(
        ts="2026-08-11T10:00:00Z", lang="EN", store_id=SIBLING,
        question=f"{MARK} sibling secret question", answer="sibling answer",
        cached=False, latency_ms=5000, embed_id="embSIB", path="agent",
    )
    for n, site in enumerate(DECOYS):
        # Asked TWICE at the decoy branch, so it is a repeat in its own right —
        # otherwise /analytics/repeats' `HAVING count(*) > 1` would hide it from a
        # substring matcher too and that scope test could not fail.
        for _ in range(2):
            _insert(
                ts="2026-08-11T10:01:00Z", lang="EN", store_id=site,
                question=f"{MARK} decoy secret question {n}", answer="decoy answer",
                cached=False, latency_ms=5000, embed_id=f"embDECOY{n}", path="agent",
            )
    yield {"mark": MARK, "mine": MINE, "sibling": SIBLING, "decoys": DECOYS}
    _purge()


def _purge():
    _pg("DELETE FROM chat_logs WHERE question LIKE $1", f"%{MARK}%")


class _Admin:
    """An approved admin account + a bearer header, optionally pinned to a branch."""

    def __init__(self, role="admin", store_id=None):
        _ensure_schema()
        self.email = f"anl-{uuid.uuid4().hex[:10]}@corp.mm"
        rows = _pg(
            """INSERT INTO users (email, name, role, auth_sources, active, approved, store_id)
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
    a = _Admin()
    yield a
    a.drop()


def _summary(client, headers, **params):
    r = client.get("/admin/analytics/summary", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _questions(client, headers, **params):
    r = client.get("/admin/analytics/questions", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---- 1. the aggregates are true -------------------------------------------


def test_summary_counts_and_rates(api_client, seeded_logs, admin):
    """turns / distinct / repeat_rate / cache_hits / cache_rate over seeded rows."""

    s = _summary(api_client, admin.headers, q=MARK, store=MINE)
    assert s["turns"] == 8
    assert s["distinct"] == 7                 # two rows share one question text
    assert s["repeat_rate"] == round(1 - 7 / 8, 4)
    assert s["cache_hits"] == 2
    assert s["cache_rate"] == round(2 / 8, 4)


def test_summary_percentiles(api_client, seeded_logs, admin):
    """p50/p95 come from percentile_cont over the eight seeded latencies.

    Sorted: 3, 50, 4000, 6000, 9000, 12000, 21000, 30000.
    percentile_cont interpolates at rank p*(n-1):
      0.5  -> rank 3.5  -> midpoint of 6000 and 9000              = 7500
      0.95 -> rank 6.65 -> 21000 + 0.65*(30000-21000)             = 26850
    """

    s = _summary(api_client, admin.headers, q=MARK, store=MINE)
    assert s["p50_ms"] == 7500
    assert s["p95_ms"] == 26850


def test_summary_buckets_are_disjoint_and_total(api_client, seeded_logs, admin):
    s = _summary(api_client, admin.headers, q=MARK, store=MINE)
    assert s["buckets"] == {
        "lt100": 2,        # 3, 50
        "lt5000": 1,       # 4000
        "lt10000": 2,      # 6000, 9000
        "lt20000": 1,      # 12000
        "gte20000": 2,     # 21000, 30000
    }
    assert sum(s["buckets"].values()) == s["turns"]


def test_summary_groups_by_lang_store_and_day(api_client, seeded_logs, admin):
    s = _summary(api_client, admin.headers, q=MARK, store=MINE)

    langs = {(r["lang"], r["cached"]): r["n"] for r in s["by_lang"]}
    assert langs[("EN", True)] == 2
    assert langs[("EN", False)] == 4
    assert langs[("MY", False)] == 2

    assert [r["store_id"] for r in s["by_store"]] == [MINE]
    assert s["by_store"][0]["n"] == 8

    days = {r["day"]: r["n"] for r in s["by_day"]}
    assert days == {"2026-08-10": 3, "2026-08-11": 3, "2026-08-12": 2}


def test_summary_records_an_empty_answer_as_a_refusal(api_client, admin):
    """A turn that produced nothing must be COUNTED, not silently absent.

    `if full:` used to skip the write entirely, so the audit log could only ever
    show the system working — the failures left no row at all.
    """

    _ensure_schema()
    _purge()
    _insert(ts="2026-08-10T09:00:00Z", store_id=MINE, question=f"{MARK} nothing came back",
            answer="", latency_ms=8000, path="agent")
    _insert(ts="2026-08-10T09:01:00Z", store_id=MINE, question=f"{MARK} this one worked",
            answer="here you go", latency_ms=8000, path="agent")
    try:
        s = _summary(api_client, admin.headers, q=MARK)
        assert s["turns"] == 2
        assert s["refusals"] == 1
    finally:
        _purge()


def test_summary_on_no_matching_rows_is_zero_not_a_division_error(api_client, admin):
    s = _summary(api_client, admin.headers, q="zz-no-such-question-zz")
    assert s["turns"] == 0
    assert s["repeat_rate"] == 0.0 and s["cache_rate"] == 0.0
    assert s["p50_ms"] is None and s["p95_ms"] is None
    assert s["by_lang"] == [] and s["by_day"] == []


# ---- 2. the filters actually filter ---------------------------------------


@pytest.mark.parametrize(
    "params, expected",
    [
        ({"from": "2026-08-11"}, 5),                       # 11th (3) + 12th (2)
        ({"to": "2026-08-10"}, 3),                         # the whole 10th
        ({"from": "2026-08-11", "to": "2026-08-11"}, 3),   # one day, inclusive
        ({"from": "2026-08-13"}, 0),
    ],
)
def test_from_and_to_filter_the_window(api_client, seeded_logs, admin, params, expected):
    """A BARE DATE in `to` must mean the whole day.

    `ts <= '2026-08-11'` is midnight, so it would drop every turn of that day and
    read as "no traffic" — the bound is exclusive-next-day instead.
    """

    s = _summary(api_client, admin.headers, q=MARK, store=MINE, **params)
    assert s["turns"] == expected


def test_a_timestamped_to_is_exclusive(api_client, seeded_logs, admin):
    """A `to` carrying a TIME excludes that instant. Changed 2026-08-17.

    This test previously asserted 2 — a timestamped `to` was inclusive (`<=`)
    while `to` given as a bare date meant the whole day. Adding `start`/`end`
    alongside made that inconsistency visible: the same calendar day meant two
    different windows depending on which spelling and which precision a caller
    used, and the six new endpoints disagreed with the eight old ones.

    Both spellings now share one bound (`_upper_bound`):

    * bare date  -> the whole day is included (`ts < date + 1 day`)
    * with time  -> exclusive at that instant

    Bare dates are unchanged, which is what every date picker sends. This only
    moves the boundary for a caller passing an explicit time, and it moves it
    toward the half-open convention the rest of the API already uses.

    Seeded turns are at 09:00, 09:01 and 09:02, so [09:01, 09:02) holds exactly
    the 09:01 turn.
    """

    s = _summary(
        api_client, admin.headers, q=MARK, store=MINE,
        **{"from": "2026-08-10T09:01:00Z", "to": "2026-08-10T09:02:00Z"},
    )
    assert s["turns"] == 1

    # And the same window spelled the new way agrees, which is the whole point.
    s2 = _summary(
        api_client, admin.headers, q=MARK, store=MINE,
        start="2026-08-10T09:01:00Z", end="2026-08-10T09:02:00Z",
    )
    assert s2["turns"] == s["turns"]


def test_a_malformed_date_is_a_400_naming_the_parameter(api_client, admin):
    r = api_client.get(
        "/admin/analytics/summary", params={"from": "last tuesday"}, headers=admin.headers
    )
    assert r.status_code == 400
    assert "from" in r.json()["detail"]


def test_q_matches_the_question_and_the_answer(api_client, seeded_logs, admin):
    """Free text searches BOTH columns — an operator hunting a drug name will as
    often remember what the agent said as what the customer typed."""

    hit_q = _questions(api_client, admin.headers, q=f"{MARK} question 5", store=MINE)
    assert hit_q["total"] == 1

    # "lidocaine" appears ONLY in row 3's answer, never in any question.
    hit_a = _questions(api_client, admin.headers, q="lidocaine", store=MINE)
    assert hit_a["total"] == 1
    assert MARK in hit_a["rows"][0]["question"]
    assert "lidocaine" in hit_a["rows"][0]["answer"]


def test_lang_and_embed_filters(api_client, seeded_logs, admin):
    assert _summary(api_client, admin.headers, q=MARK, store=MINE, lang="MY")["turns"] == 2
    assert _summary(api_client, admin.headers, q=MARK, store=MINE, embed="embA")["turns"] == 4


# ---- 3. pagination is honest ----------------------------------------------


def test_questions_total_is_the_unpaged_count(api_client, seeded_logs, admin):
    """`total` must ignore limit/offset — that is what makes a pager truthful.

    GET /admin/conversations returns a bare list, so a UI over it cannot tell
    "page 1 of 4" from "that is everything" without asking for a row it will not
    show.
    """

    page = _questions(api_client, admin.headers, q=MARK, store=MINE, limit=3)
    assert page["total"] == 8
    assert len(page["rows"]) == 3

    page2 = _questions(api_client, admin.headers, q=MARK, store=MINE, limit=3, offset=6)
    assert page2["total"] == 8
    assert len(page2["rows"]) == 2

    ids = {r["id"] for r in page["rows"]} | {r["id"] for r in page2["rows"]}
    assert len(ids) == 5          # disjoint pages, no row served twice


def test_questions_returns_the_audit_columns(api_client, seeded_logs, admin):
    rows = _questions(api_client, admin.headers, q=f"{MARK} question 4", store=MINE)["rows"]
    assert len(rows) == 1
    r = rows[0]
    assert r["embed_id"] == "embB"
    assert r["model"] == "google/gemini-3.5-flash"
    assert r["tools"] == ["get_stock"]          # JSONB decoded, not a raw string
    assert r["path"] == "agent"
    assert r["latency_ms"] == 9000
    # The list shape is exactly what the page renders — no session key here.
    assert set(r) == {
        "id", "ts", "question", "answer", "lang", "store_id", "embed_id",
        "model", "tools", "cached", "path", "latency_ms",
    }


def test_question_detail_and_404(api_client, seeded_logs, admin):
    row = _questions(api_client, admin.headers, q=f"{MARK} question 4", store=MINE)["rows"][0]
    r = api_client.get(f"/admin/analytics/question/{row['id']}", headers=admin.headers)
    assert r.status_code == 200
    assert r.json()["id"] == row["id"]
    assert r.json()["tools"] == ["get_stock"]
    # The single-turn view adds the conversation key the list omits.
    assert r.json()["session_id"] == "sess4"

    assert api_client.get(
        "/admin/analytics/question/999999999", headers=admin.headers
    ).status_code == 404


# ---- 4. store scoping — the decoy tests -----------------------------------


def test_questions_scope_hides_a_sibling_branch(api_client, seeded_logs):
    a = _Admin(store_id=MINE)
    try:
        body = _questions(api_client, a.headers, q=MARK, limit=200)
        stores = {r["store_id"] for r in body["rows"]}
        assert stores == {MINE}
        assert body["total"] == 8
        assert not any("sibling secret" in r["question"] for r in body["rows"])
    finally:
        a.drop()


@pytest.mark.parametrize("token_form", ["20005", "CCYK", "20005-CCYK"])
def test_questions_scope_does_not_substring_match_a_sibling(
    api_client, seeded_logs, token_form
):
    """The scope must match ANCHORED, not as a substring.

    ``200059-CCZZ`` CONTAINS ``20005`` but is a different branch. `_site_clause`
    excludes it; the documented-bad ``store_id ILIKE '%'||$n||'%'`` returns it,
    which is one pharmacy reading another's customer questions. The seeded MINE /
    SIBLING codes are substring-disjoint from the tokens, so without this decoy
    the test could not tell a correct matcher from a wrong one.
    """

    a = _Admin(store_id=token_form)
    try:
        body = _questions(api_client, a.headers, q=MARK, limit=200)
        assert {r["store_id"] for r in body["rows"]} == {MINE}
        assert body["total"] == 8
        assert not any("decoy secret" in r["question"] for r in body["rows"])
    finally:
        a.drop()


@pytest.mark.parametrize("token_form", ["20005", "CCYK"])
def test_summary_scope_does_not_substring_match_a_sibling(api_client, seeded_logs, token_form):
    a = _Admin(store_id=token_form)
    try:
        s = _summary(api_client, a.headers, q=MARK)
        assert s["turns"] == 8                         # never 9 or 10
        assert [r["store_id"] for r in s["by_store"]] == [MINE]
        assert s["store_scope"] == token_form
    finally:
        a.drop()


@pytest.mark.parametrize("token_form", ["20005", "CCYK"])
def test_embeds_scope_does_not_substring_match_a_sibling(api_client, seeded_logs, token_form):
    """The per-embed rollup leaks a branch's embed IDS and traffic if unscoped."""

    a = _Admin(store_id=token_form)
    try:
        rows = api_client.get(
            "/admin/analytics/embeds", params={"q": MARK}, headers=a.headers
        ).json()
        assert {r["store_id"] for r in rows} == {MINE}
        embeds = {r["embed_id"] for r in rows}
        assert "embSIB" not in embeds
        assert not any((e or "").startswith("embDECOY") for e in embeds)
    finally:
        a.drop()


def test_question_detail_scope_404s_on_a_sibling_row(api_client, seeded_logs, admin):
    """Guessing a sibling's row id must 404 — the same answer as a missing id.

    Fetching and then hiding would leak the row's existence via the status code.
    """

    sibling_row = _questions(
        api_client, admin.headers, q="sibling secret", limit=5
    )["rows"][0]

    a = _Admin(store_id=MINE)
    try:
        r = api_client.get(
            f"/admin/analytics/question/{sibling_row['id']}", headers=a.headers
        )
        assert r.status_code == 404
    finally:
        a.drop()


def test_store_filter_cannot_widen_past_the_scope(api_client, seeded_logs):
    """`store` is the operator's filter box; `scope` is a boundary. ANDed, never
    substituted — a pinned caller who types a sibling's code gets nothing."""

    a = _Admin(store_id=MINE)
    try:
        body = _questions(api_client, a.headers, q=MARK, store=SIBLING)
        assert body["total"] == 0 and body["rows"] == []
    finally:
        a.drop()


def test_unscoped_admin_sees_every_branch(api_client, seeded_logs, admin):
    body = _questions(api_client, admin.headers, q=MARK, limit=200)
    assert body["total"] == 13          # 8 mine + 1 sibling + 2x2 decoy
    assert {r["store_id"] for r in body["rows"]} == {MINE, SIBLING, *DECOYS}


def test_super_admin_is_never_scoped(api_client, seeded_logs):
    a = _Admin(role="super_admin", store_id=MINE)
    try:
        s = _summary(api_client, a.headers, q=MARK)
        assert s["turns"] == 13
        assert s["store_scope"] is None
    finally:
        a.drop()


# ---- 5. unattributed rows stay unattributed -------------------------------


def test_embeds_reports_a_null_embed_id_as_null(api_client, seeded_logs, admin):
    """Pre-migration turns carry NULL embed_id. That is the honest label.

    Two of the seeded rows have no embed. They must come back as `null` and be
    counted separately — folding them into a real embed, or relabelling them
    "unknown" so they sort among real ids, would make the one column this
    endpoint exists for untrustworthy.
    """

    rows = api_client.get(
        "/admin/analytics/embeds", params={"q": MARK, "store": MINE}, headers=admin.headers
    ).json()
    by_embed = {r["embed_id"]: r for r in rows}

    assert None in by_embed                       # not "unknown", not ""
    assert by_embed[None]["turns"] == 2
    assert by_embed["embA"]["turns"] == 4
    assert by_embed["embA"]["cache_rate"] == 0.5      # 2 of embA's 4 were cached
    assert by_embed["embB"]["turns"] == 2
    assert by_embed["embB"]["p50_ms"] == 10500     # midpoint of 9000 and 12000
    assert by_embed["embA"]["last_seen"] is not None


def test_questions_reports_a_null_embed_id_as_null(api_client, seeded_logs, admin):
    rows = _questions(api_client, admin.headers, q=f"{MARK} question 6", store=MINE)["rows"]
    assert rows[0]["embed_id"] is None
    assert rows[0]["tools"] == ["get_stock"]


# ---- 6. repeats -----------------------------------------------------------


def test_repeats_groups_on_normalised_question_text(api_client, admin):
    _ensure_schema()
    _purge()
    for ms, cached, text in (
        (10, True, f"{MARK} Do You Have Aspirin"),
        (20, True, f"  {MARK} do you have aspirin  "),
        (5000, False, f"{MARK} DO YOU HAVE ASPIRIN"),
        (5000, False, f"{MARK} asked once only"),
    ):
        _insert(ts="2026-08-10T09:00:00Z", store_id=MINE, question=text,
                answer="a", cached=cached, latency_ms=ms, path="agent")
    try:
        rows = api_client.get(
            "/admin/analytics/repeats", params={"q": MARK, "store": MINE}, headers=admin.headers
        ).json()
        assert len(rows) == 1                       # the once-only row is not a repeat
        assert rows[0]["question"] == f"{MARK} do you have aspirin".lower()
        assert rows[0]["asked"] == 3
        assert rows[0]["cached"] == 2
        assert rows[0]["median_ms"] == 20
    finally:
        _purge()


@pytest.mark.parametrize("token_form", ["20005", "CCYK"])
def test_repeats_is_scoped(api_client, seeded_logs, token_form):
    """The decoy questions are seeded TWICE each, so they clear `HAVING count > 1`
    and a substring matcher really would surface them here."""

    a = _Admin(store_id=token_form)
    try:
        rows = api_client.get(
            "/admin/analytics/repeats", params={"q": MARK}, headers=a.headers
        ).json()
        assert all("decoy" not in r["question"] for r in rows)
        assert all("sibling" not in r["question"] for r in rows)
        assert [r["question"] for r in rows] == [f"{MARK} do you have paracetamol".lower()]
    finally:
        a.drop()


# ---- 7. data health -------------------------------------------------------


def test_data_health_shape(api_client, admin):
    r = api_client.get("/admin/analytics/data-health", headers=admin.headers)
    assert r.status_code == 200
    body = r.json()
    # `by_day` added 2026-08-17 for the Data health tab's ingest chart. It is
    # `null` — not `[]` — when ingest_events cannot be read: an empty list
    # claims no ingest happened, null says we cannot see, and only one of those
    # should send somebody to go and look at the SFTP box.
    # `funnel`/`funnel_meta` (contract §F2) and `tz` (§F1) added 2026-08-17.
    # This assertion is an EXACT key set on purpose — it catches a key vanishing
    # as loudly as one appearing — so a payload addition mandated by the contract
    # updates it here rather than loosening it to a subset check.
    assert set(body) == {"catalog", "inventory", "freshness", "by_day",
                         "funnel", "funnel_meta", "tz"}
    assert set(body["catalog"]) == {"total", "stubs", "stub_ratio"}
    assert set(body["inventory"]) == {
        "rows", "sites", "zero", "negative", "under_20", "null_qty"
    }
    assert set(body["freshness"]) == {"catalog_at", "inventory_at"}
    assert body["catalog"]["total"] > 0
    assert body["inventory"]["rows"] > 0


def test_data_health_inventory_half_is_scoped(api_client):
    """The catalog is one shared product list and is NOT scoped; inventory counts
    rows per site and IS."""

    a = _Admin(store_id=MINE)
    b = _Admin()
    try:
        scoped = api_client.get("/admin/analytics/data-health", headers=a.headers).json()
        full = api_client.get("/admin/analytics/data-health", headers=b.headers).json()
        assert scoped["inventory"]["sites"] == 1
        assert full["inventory"]["sites"] > 1
        assert scoped["inventory"]["rows"] < full["inventory"]["rows"]
        # catalog is global — identical for both callers
        assert scoped["catalog"] == full["catalog"]
    finally:
        a.drop()
        b.drop()


# ---- 8. logging must never break an answer --------------------------------


def test_log_chat_never_raises_when_the_write_fails():
    """A logging failure costs a log row, never a reply.

    ``log_chat`` sits on the answer path. Everything else in this file is about
    the audit trail being complete; this one is about the audit trail never being
    the reason a pharmacy gets no answer — the same ordering ``ingest_events``
    and ``history.record_turn`` are built on.
    """

    from app import admin as adminmod

    async def boom(*a, **kw):
        raise RuntimeError("postgres is down")

    async def go():
        with mock.patch.object(adminmod, "q", boom):
            await adminmod.log_chat(
                "q", "a", MINE, False, 12,
                embed_id="e", session_id="s", model="m", tools=["t"], path="agent",
            )

    asyncio.run(go())     # must not raise


def test_log_chat_falls_back_when_the_audit_columns_are_missing():
    """On a database where ensure_chat_logs has not run, the turn is still
    recorded — unattributed, but recorded. A schema drift must not become a
    silent hole in the log on top of everything else it breaks."""

    from app import admin as adminmod

    calls = []

    async def fake_q(sql, *args):
        calls.append(sql)
        if "embed_id" in sql:
            raise RuntimeError('column "embed_id" of relation "chat_logs" does not exist')
        return []

    async def go():
        with mock.patch.object(adminmod, "q", fake_q):
            await adminmod.log_chat("q", "a", MINE, False, 12, embed_id="e", path="agent")

    asyncio.run(go())
    # The ladder walks back one schema GENERATION at a time — turn metrics
    # (0006), then the audit columns (0003), then the original six — so a
    # database missing only the newest columns still records the older ones.
    # This fake rejects anything naming `embed_id`, i.e. the oldest schema.
    assert len(calls) == 3
    assert "input_tokens" in calls[0]
    assert "embed_id" in calls[1] and "input_tokens" not in calls[1]
    assert "embed_id" not in calls[2]


def test_log_chat_writes_the_audit_columns(api_client, admin):
    """End to end through the real endpoint: what log_chat stores is what
    /analytics/questions reads back."""

    _ensure_schema()
    _purge()
    from app import admin as adminmod
    from app import db as dbmod
    from app.db import close_pool

    # The api_client fixture has already opened a pool on the TestClient's portal
    # loop. Dropping the reference forces `log_chat` to build its OWN pool on the
    # loop asyncio.run is about to create — reusing the portal's would raise
    # "attached to a different loop" from inside close_pool.
    dbmod._pool = None

    async def go():
        try:
            await adminmod.log_chat(
                f"{MARK} round trip", "the answer", MINE, False, 4321,
                embed_id="embRT", session_id="sessRT", model="mdl",
                tools=["search_by_name", "get_stock"], path="agent",
            )
        finally:
            await close_pool()

    asyncio.run(go())
    dbmod._pool = None          # and hand the next request a fresh one
    try:
        rows = _questions(api_client, admin.headers, q=f"{MARK} round trip")["rows"]
        assert len(rows) == 1
        r = rows[0]
        assert r["embed_id"] == "embRT"
        assert r["model"] == "mdl" and r["path"] == "agent"
        assert r["tools"] == ["search_by_name", "get_stock"]
        assert r["latency_ms"] == 4321
        assert r["lang"] == "EN"

        # session_id only appears on the single-turn view.
        one = api_client.get(
            f"/admin/analytics/question/{r['id']}", headers=admin.headers
        ).json()
        assert one["session_id"] == "sessRT"
    finally:
        _purge()


# ---- 9. retention ---------------------------------------------------------


def _retention_probe(days: int) -> tuple[int, int]:
    """Seed one ancient + one fresh row, prune with `days`, return (removed, left)."""

    from app import admin as adminmod
    from app.db import close_pool

    _ensure_schema()
    _purge()
    _insert(ts="2020-01-01T00:00:00Z", store_id=MINE,
            question=f"{MARK} ancient", answer="a", latency_ms=1)
    _insert(ts="2026-08-12T00:00:00Z", store_id=MINE,
            question=f"{MARK} fresh", answer="a", latency_ms=1)

    base = get_settings().model_dump()

    async def go():
        try:
            with mock.patch(
                "app.admin.get_settings",
                return_value=Settings(**{**base, "chat_log_retention_days": days}),
            ):
                return await adminmod.prune_chat_logs()
        finally:
            await close_pool()

    removed = asyncio.run(go())
    left = _pg(
        "SELECT count(*) AS n FROM chat_logs WHERE question LIKE $1", f"%{MARK}%", fetch=True
    )[0]["n"]
    _purge()
    return removed, left


def test_retention_zero_keeps_forever():
    """`chat_log_retention_days = 0` must skip the DELETE entirely.

    Not "delete rows older than 0 days", which is every row — an operator opting
    OUT of retention must not be the one who wipes the log.
    """

    removed, left = _retention_probe(0)
    assert removed == 0
    assert left == 2                 # the 2020 row survives


def test_retention_default_still_deletes():
    """The default (30) is unchanged: an ancient row still goes.

    Without this half the test above passes on a prune that does nothing at all.
    """

    assert Settings.model_fields["chat_log_retention_days"].default == 30
    removed, left = _retention_probe(30)
    assert removed >= 1
    assert left == 1                 # only the fresh row remains
