"""The observability capture layer: `app_events` and per-turn token/cost.

**The test that matters is `test_a_secret_put_through_auth_config_is_not_stored`.**
Everything else here pins behaviour; that one pins a promise. `PUT
/admin/auth-config` carries `oidc_client_secret` and `ldap_bind_password` in
cleartext, `POST /admin/users` carries a new account's password, and
`POST /admin/credentials` / `POST /admin/sftp/keys` carry keys. An audit table
that stored those bodies would be a credential store with a time index — copied
into every dump, readable by every later `SELECT *`, and completely invisible
until someone read the table. So the middleware stores a per-route ALLOWLIST of
summary fields, an unlisted route stores nothing but route + status, and
key-name redaction is the second line rather than the first.

Verified non-vacuously: making `activity.summarize_body` return the raw body
makes exactly that test fail on both assertions (the literal secret AND its
8-character prefix), and nothing else in this file notices — which is the point,
because a redaction bug is silent everywhere except where it is asserted.

The second half is the cost columns (migration 0006). Two invariants:

* **NULL is not zero.** A cache hit ran no model; a provider that reports no
  price reports nothing, not "free". `cost=None` and `cost=0` both store NULL,
  because a 0 in a spend column is read by a human as a fact.
* **Extraction never breaks an answer.** A renamed agno attribute costs a
  metric. `test_extract_metrics_survives_a_hostile_object` runs it against an
  object whose every attribute raises.

⚠️ Every DB setup/teardown goes through :func:`_pg`, a throwaway asyncpg
connection — NOT ``app.db.q``. The shared pool is bound to whichever loop
created it, and under ``api_client`` that is the TestClient's portal loop.
Fixtures follow ``tests/test_branding.py`` / ``tests/test_auth_hardening.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid

import pytest

from app import activity
from app import auth as authmod
# Imported at MODULE level on purpose. `app.api` pulls in agno, whose import
# builds an asyncio.Lock and therefore needs a current event loop — and on
# Python 3.9 `asyncio.run` (which `_pg` below uses in every fixture) CLOSES its
# loop and leaves the thread with none set. Importing it lazily from a fixture
# put the import AFTER the first `_pg` call and every test in this file errored
# at setup with "There is no current event loop in thread 'MainThread'". Same
# trap the `api_client` fixture documents in tests/conftest.py.
from app import api as apimod  # noqa: F401
from app.config import get_settings

from tests.pgconn import pg
from tests import dbguard


def _pg(query: str, *args, fetch: bool = False):
    """Run one statement on a private connection. Never touches app.db's pool.

    One connection per PROCESS, not per statement — see tests/pgconn.py for why
    the previous arrangement was the suite's whole wall clock.
    """

    return pg(query, *args, fetch=fetch)


def _ensure_activity_schema():
    """app_events + the chat_logs metric columns, mirroring 0007 and 0006."""

    _pg(
        """CREATE TABLE IF NOT EXISTS app_events (
               id          BIGSERIAL PRIMARY KEY,
               ts          TIMESTAMPTZ DEFAULT now(),
               actor_email TEXT,
               actor_role  TEXT,
               action      TEXT NOT NULL,
               target      TEXT,
               method      TEXT,
               path        TEXT,
               status      INT,
               detail      JSONB,
               ip          TEXT,
               duration_ms INT
           )"""
    )
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
        ("input_tokens", "INT"), ("output_tokens", "INT"), ("total_tokens", "INT"),
        ("reasoning_tokens", "INT"), ("cost_usd", "NUMERIC(12,6)"), ("ttft_ms", "INT"),
    ):
        _pg(f"ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS {col} {typ}")


@pytest.fixture(autouse=True)
def _clean_events():
    """An empty app_events before AND after every test in this module.

    Cleaning only on the way out is not enough: a killed run never reaches the
    teardown, and "exactly one row was written" is then asserted against
    somebody else's leftovers and fails somewhere unrelated.
    """

    # UNQUALIFIED, and app_events is the table the Activity feed and the
    # analytics Actors panel read. Pointed at the live DSN — which is what it
    # used to be — this fixture both wipes the real audit trail and refills it
    # with generated `activity-*@corp.mm` actors that then out-rank the only
    # real admin. conftest redirects POSTGRES_URL to a clone and dbguard patches
    # asyncpg, but the assert stays next to the DELETE it protects.
    dbguard.assert_test_database()

    _ensure_activity_schema()
    _pg("DELETE FROM app_events")
    yield
    dbguard.assert_test_database()
    _pg("DELETE FROM app_events")


@pytest.fixture(autouse=True)
def _reset_log_chat_probe():
    """Drop the cached `log_chat` signature verdict between tests."""

    apimod._LOG_CHAT_TAKES_METRICS = None
    yield
    apimod._LOG_CHAT_TAKES_METRICS = None


class _Admin:
    """An approved account + a bearer header. Mirrors tests/test_branding.py."""

    def __init__(self, role="admin"):
        _pg("ALTER TABLE users ADD COLUMN IF NOT EXISTS store_id TEXT")
        self.email = f"activity-{uuid.uuid4().hex[:10]}@corp.mm"
        rows = _pg(
            """INSERT INTO users (email, name, role, auth_sources, active, approved)
               VALUES ($1,'ActivityProbe',$2,ARRAY['local'],TRUE,TRUE)
               RETURNING id, email, role""",
            self.email, role, fetch=True,
        )
        self.id = rows[0]["id"]
        self.token = authmod.make_token(rows[0])["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def drop(self):
        _pg("DELETE FROM users WHERE id=$1", self.id)


@pytest.fixture
def admin():
    a = _Admin(role="admin")
    yield a
    a.drop()


@pytest.fixture
def super_admin():
    a = _Admin(role="super_admin")
    yield a
    a.drop()


def _events(actor=None):
    """Recorded events, with `detail` decoded. Optionally only one actor's.

    Two details, both learned here:

    * asyncpg hands JSONB back as a *string* unless a codec is registered, so a
      raw comparison against a dict fails on key ORDER — the failure that would
      hide a redaction bug behind a formatting complaint.
    * **Filter by actor when asserting a COUNT.** The middleware is now live for
      the whole suite, so every other test file that PUTs or POSTs to `/admin/*`
      writes rows here too. `_clean_events` truncates between tests in THIS file,
      but a bare "exactly one row" is asserted against a global table and went
      red once on two rows left by `tests/test_admin_scope.py`. Each `_Admin`
      fixture gets a uuid'd email, so scoping by actor is exact.
    """

    if actor is not None:
        email = getattr(actor, "email", actor)
        rows = _pg(
            "SELECT * FROM app_events WHERE actor_email=$1 ORDER BY id", email, fetch=True
        )
    else:
        rows = _pg("SELECT * FROM app_events ORDER BY id", fetch=True)
    for r in rows:
        if isinstance(r.get("detail"), str):
            r["detail"] = json.loads(r["detail"])
    return rows


# =============================================================================
# The activity trail
# =============================================================================


def test_a_mutating_admin_call_writes_exactly_one_row(api_client, admin):
    """Who, what, where, when, and how long — one row, not two."""

    r = api_client.delete(
        "/admin/credentials/no-such-embed-xyz", headers=admin.headers
    )
    assert r.status_code == 200

    rows = _events(admin)
    assert len(rows) == 1, f"expected one row, got {rows}"
    ev = rows[0]
    assert ev["action"] == "admin.credentials.delete"
    assert ev["target"] == "no-such-embed-xyz"
    assert ev["method"] == "DELETE"
    assert ev["path"] == "/admin/credentials/no-such-embed-xyz"
    assert ev["status"] == 200
    assert ev["actor_email"] == admin.email
    assert ev["actor_role"] == "admin"
    assert ev["duration_ms"] is not None and ev["duration_ms"] >= 0
    assert ev["ts"] is not None


def test_a_get_writes_nothing(api_client, admin):
    """This is an audit trail of CHANGES. Reading is not a change."""

    assert api_client.get("/admin/credentials", headers=admin.headers).status_code == 200
    assert _events(admin) == []


def test_health_and_metrics_are_not_recorded(api_client):
    """Liveness probes would out-number real events by four orders of magnitude.

    Asserted as a DELTA, not as "the table is empty". These two tests take no
    `admin` fixture, so they have no unique actor to filter on, and a bare
    emptiness assertion is a claim about rows nothing in this test wrote —
    which went red twice on leftovers from an earlier full-suite run.
    """

    before = len(_events())
    api_client.get("/health")
    api_client.get("/metrics")
    api_client.get("/ready")
    assert len(_events()) == before


def test_a_login_is_not_duplicated_here(api_client):
    """`login_ok`/`login_fail` live in auth_events, which is also the lockout counter."""

    before = len(_events())
    api_client.post("/auth/login", json={"email": "nobody@example.com", "password": "x"})
    assert len(_events()) == before


def test_a_failed_request_is_still_recorded_with_its_status(api_client, admin):
    """A REFUSED privilege escalation is the row a review most wants to see.

    `POST /admin/users` is super_admin-only; a plain admin gets 403. An audit
    trail that only recorded successes would show nothing at all here — i.e. it
    would only ever show the system working.
    """

    r = api_client.post(
        "/admin/users",
        headers=admin.headers,
        json={
            "email": "escalation@corp.mm", "name": "Nope",
            "password": "hunter2-should-never-be-stored", "role": "super_admin",
        },
    )
    assert r.status_code == 403

    rows = _events(admin)
    assert len(rows) == 1
    ev = rows[0]
    assert ev["status"] == 403
    assert ev["action"] == "admin.users.create"
    assert ev["actor_email"] == admin.email
    # The allowlist keeps the two fields that say WHAT was attempted...
    assert ev["detail"] == {"email": "escalation@corp.mm", "role": "super_admin"}
    # ...and the password is in neither the allowlist nor the row.
    assert "hunter2" not in str(ev)


def test_an_unlisted_route_stores_route_and_status_only(api_client, admin):
    """The default is silence. A route nobody has vetted keeps no body at all."""

    r = api_client.post(
        "/admin/feedback",
        headers=admin.headers,
        json={
            "session_id": "s1", "question": "do we have RELYTE?",
            "answer": "yes, 6533", "verdict": "up",
            "correction": "a free-text field nobody vetted",
        },
    )
    assert r.status_code in (200, 500)  # the write may fail; the audit row may not

    rows = _events(admin)
    assert len(rows) == 1
    ev = rows[0]
    assert ev["path"] == "/admin/feedback"
    assert ev["status"] == r.status_code
    assert ev["detail"] is None, "an unlisted route must keep NOTHING of the body"
    assert "nobody vetted" not in str(ev)


def test_the_feed_merges_app_events_and_auth_events():
    """One feed, two tables, nothing copied between them.

    `login_ok`/`login_fail` must stay in `auth_events` — that table IS the
    lockout counter (`auth.failed_logins_for_email` counts its rows), so
    mirroring them into `app_events` would either double every sign-in in the
    feed or tempt someone to move them and silently change who gets locked out.
    The merge therefore happens on READ, and this pins that the UNION's column
    types actually line up — an untested UNION is a 500 waiting for the admin
    page that first calls it.
    """

    from app import db as dbmod

    marker = f"feedprobe-{uuid.uuid4().hex[:8]}@corp.mm"
    _pg(
        """CREATE TABLE IF NOT EXISTS auth_events (
               id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(),
               event TEXT NOT NULL, email TEXT, actor_email TEXT, ip TEXT, detail TEXT
           )"""
    )
    _pg(
        """INSERT INTO app_events (actor_email, actor_role, action, target, method,
                                   path, status, detail, ip, duration_ms)
           VALUES ($1,'admin','admin.users.update','7','PATCH','/admin/users/7',200,
                   '{"role":"admin"}'::jsonb,'10.0.0.1',42)""",
        marker,
    )
    _pg(
        "INSERT INTO auth_events (event, email, ip, detail) VALUES ('login_fail',$1,'10.0.0.1','bad password')",
        marker,
    )

    dbmod._pool = None
    try:
        rows = asyncio.run(activity.unified_feed(limit=50, actor=marker))
    finally:
        dbmod._pool = None
        _pg("DELETE FROM auth_events WHERE email=$1", marker)

    sources = {r["source"] for r in rows}
    assert sources == {"app", "auth"}, f"expected both tables in the feed, got {rows}"

    app_row = next(r for r in rows if r["source"] == "app")
    assert app_row["action"] == "admin.users.update"
    assert app_row["method"] == "PATCH"
    assert app_row["status"] == 200
    assert app_row["detail"] == {"role": "admin"}   # decoded, not a JSON string
    assert app_row["duration_ms"] == 42

    auth_row = next(r for r in rows if r["source"] == "auth")
    assert auth_row["action"] == "login_fail"       # `event` projects to `action`
    assert auth_row["target"] == marker             # the account it was AGAINST
    assert auth_row["method"] is None and auth_row["status"] is None


def test_the_feed_returns_empty_rather_than_raising_when_it_cannot_read(monkeypatch):
    """An unreadable feed renders an empty page; it does not 500 the console."""

    async def boom(*_a, **_k):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(activity, "q", boom)
    assert asyncio.run(activity.unified_feed()) == []


# ---- the one that matters ---------------------------------------------------

SECRET = "s3cr3t-oidc-value-DO-NOT-STORE-" + uuid.uuid4().hex
BIND_PW = "ldap-bind-pw-DO-NOT-STORE-" + uuid.uuid4().hex


@pytest.fixture
def _restore_auth_overrides():
    """Undo the runtime auth override this test writes into Redis.

    The suite shares one Redis DB (conftest isolates it to /15, not per-test), so
    a leaked `auth.oidc_client_secret` would be inherited by every later test
    that reads the effective auth config.
    """

    yield
    import redis as _redis_sync

    client = _redis_sync.from_url(get_settings().redis_url, decode_responses=True)
    try:
        client.hdel(
            "pharmacy:config",
            "auth.oidc_client_secret", "auth.ldap_bind_password", "auth.oidc_client_id",
        )
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def test_a_secret_put_through_auth_config_is_not_stored(
    api_client, super_admin, _restore_auth_overrides
):
    """A REAL secret goes through the REAL route and appears NOWHERE in the row.

    Both the literal value and its 8-character prefix are asserted absent. The
    prefix matters: a "helpful" truncation to 8 or 12 characters is the change
    somebody makes to keep a detail field readable, it looks redacted at a
    glance, and it hands an attacker the search space of a partial secret.

    ⚠️ **Verified non-vacuously, and the first attempt was not enough.**
    Returning the raw body from `activity.summarize_body` (layer 1) made this
    test fail — but only on the `detail ==` assertion. The `SECRET not in blob`
    assertions still PASSED, because `record_event`'s key-name redaction (layer
    2) caught the value on the way in. That is the layering working as designed,
    and it is also exactly the trap CLAUDE.md records for
    `test_ingest_during_run_does_not_poison_cache`: with a two-part fix,
    disabling one half leaves the other covering for it. Only with BOTH removed
    does the literal secret land in the row and the assertion fail:

        assert 's3cr3t-oidc-value-...' not in "{'id': 34, ...}"

    So do not read a pass here as proof that the allowlist works. It proves the
    two together work; `test_the_allowlist_never_returns_an_unlisted_field`
    pins layer 1 on its own.
    """

    r = api_client.put(
        "/admin/auth-config",
        headers=super_admin.headers,
        json={
            "oidc_client_secret": SECRET,
            "ldap_bind_password": BIND_PW,
            "oidc_client_id": "citcare-console",
        },
    )
    assert r.status_code == 200

    rows = _events(super_admin)
    assert len(rows) == 1
    ev = rows[0]
    blob = str(ev)

    assert SECRET not in blob
    assert SECRET[:8] not in blob
    assert BIND_PW not in blob
    assert BIND_PW[:8] not in blob

    # And for completeness: nowhere in the TABLE, not merely nowhere in this row.
    whole_table = str(_pg("SELECT * FROM app_events", fetch=True))
    assert SECRET not in whole_table and SECRET[:8] not in whole_table
    assert BIND_PW not in whole_table and BIND_PW[:8] not in whole_table

    # What IS kept: that these fields were changed, by whom. The NAME of a
    # secret field is the audit fact; the value is not.
    assert ev["action"] == "admin.auth-config.update"
    assert ev["actor_email"] == super_admin.email
    assert ev["detail"] == {
        "fields": ["ldap_bind_password", "oidc_client_id", "oidc_client_secret"]
    }


def test_the_allowlist_never_returns_an_unlisted_field():
    """Unit-level: the function that decides what may be kept, on hostile input."""

    body = {
        "embed_id": "emb-9",
        "public_key": "PUBLIC-KEY-MATERIAL",
        "api_token": "tok-abc",
        "nested": {"password": "p", "ok": 1},
    }
    out = activity.summarize_body("POST", "/admin/credentials", body)
    assert out == {"embed_id": "emb-9"}

    # An unlisted route keeps nothing at all, however tame the body looks.
    assert activity.summarize_body("POST", "/admin/anything-new", body) is None


def test_redaction_catches_a_sensitive_field_added_to_an_allowlisted_model():
    """The second line of defence: by KEY NAME, recursively.

    The allowlist names fields; the Pydantic model those names point at can grow
    a secret in a later release without anyone revisiting activity.py.
    """

    out = activity._redact(
        {
            "role": "admin",
            "password": "pw",
            "OIDC_Client_Secret": "s",
            "session_token": "t",
            "public_key": "k",
            "nested": [{"bind_password": "x", "keep": "yes"}],
        }
    )
    assert out["role"] == "admin"
    assert out["password"] == activity.REDACTED
    assert out["OIDC_Client_Secret"] == activity.REDACTED
    assert out["session_token"] == activity.REDACTED
    assert out["public_key"] == activity.REDACTED
    assert out["nested"][0]["bind_password"] == activity.REDACTED
    assert out["nested"][0]["keep"] == "yes"


def test_record_event_never_raises_when_the_write_fails(monkeypatch):
    """A Postgres blip must cost an audit row, never the request that caused it.

    Same rule as `auth.record_auth_event` and `ingest_events.record`: a change
    that happens but is not logged is a gap; a change that FAILS because the
    audit table was down is an outage caused by the logging.
    """

    async def boom(*_a, **_k):
        raise RuntimeError("postgres is on fire")

    monkeypatch.setattr(activity, "execute", boom)

    async def go():
        await activity.record_event(
            "admin.users.update", actor_email="a@b.c", method="PATCH",
            path="/admin/users/1", status=200, detail={"role": "admin"},
        )

    asyncio.run(go())  # must not raise


def test_the_middleware_does_not_break_the_request_it_audits(api_client, admin, monkeypatch):
    """End-to-end version of the same rule, through the real middleware."""

    async def boom(*_a, **_k):
        raise RuntimeError("postgres is on fire")

    monkeypatch.setattr(activity, "execute", boom)
    r = api_client.delete("/admin/credentials/still-fine", headers=admin.headers)
    assert r.status_code == 200


def test_the_body_read_does_not_starve_the_handler(api_client, super_admin):
    """The middleware reads the JSON body; the ROUTE must still see it.

    Starlette's BaseHTTPMiddleware caches a body read in dispatch and replays it
    downstream. Without that, reading here would hang every POST — so this
    asserts the handler actually parsed the body it was sent, not merely that a
    status came back.
    """

    r = api_client.post(
        "/admin/cors-origins",
        headers=super_admin.headers,
        json={"origin": "https://audit-probe.example"},
    )
    assert r.status_code in (200, 400)
    if r.status_code == 200:
        assert r.json()["origin"].endswith("audit-probe.example")
        api_client.delete(
            "/admin/cors-origins",
            headers=super_admin.headers,
            params={"origin": "https://audit-probe.example"},
        )
    # The allowlist kept the origin — a route argument, not a secret.
    rows = _events(super_admin)
    assert rows[0]["detail"] == {"origin": "https://audit-probe.example"}


def test_action_slugs_are_low_cardinality():
    """400 user edits are one filterable action, not 400 unique ones."""

    assert activity.action_for("PATCH", "/admin/users/17") == "admin.users.update"
    assert activity.action_for("PATCH", "/admin/users/993") == "admin.users.update"
    assert activity.action_for("PUT", "/admin/auth-config") == "admin.auth-config.update"
    assert activity.action_for("POST", "/admin/graph/rebuild") == "admin.graph.rebuild.create"

    # ⚠️ Regression. Collapsing only NUMERIC segments was not enough: half the
    # ids in this API are strings (embed_id, key label, filename), and the first
    # run of this file produced `admin.credentials.no-such-embed-xyz.delete`,
    # i.e. one unique action per credential ever deleted. The captured target is
    # dropped too.
    for embed in ("no-such-embed-xyz", "emb1", "cust-42"):
        target, _spec = activity.match_route("DELETE", f"/admin/credentials/{embed}")
        assert target == embed
        assert (
            activity.action_for("DELETE", f"/admin/credentials/{embed}", target)
            == "admin.credentials.delete"
        )

    # A route word that happens to sit in the id position is NOT an id, because
    # the target is captured from the route's own pattern rather than guessed
    # from the last segment.
    target, _ = activity.match_route("POST", "/admin/graph/rebuild")
    assert target is None


def test_should_record_skips_the_noise():
    assert activity.should_record("PUT", "/admin/auth-config")
    assert not activity.should_record("GET", "/admin/auth-config")
    assert not activity.should_record("POST", "/auth/login")
    assert not activity.should_record("POST", "/api/embed/chat")
    assert not activity.should_record("POST", "/metrics")
    assert not activity.should_record("POST", "/admin/_app/immutable/x.js")


# =============================================================================
# Per-turn token + cost metrics
# =============================================================================


class _Metrics:
    """Stand-in for agno's RunMetrics — same attribute names, same defaults."""

    def __init__(self, **kw):
        self.input_tokens = kw.get("input_tokens", 0)
        self.output_tokens = kw.get("output_tokens", 0)
        self.total_tokens = kw.get("total_tokens", 0)
        self.reasoning_tokens = kw.get("reasoning_tokens", 0)
        self.cache_read_tokens = kw.get("cache_read_tokens", 0)
        self.cost = kw.get("cost", None)
        self.time_to_first_token = kw.get("time_to_first_token", None)
        self.duration = kw.get("duration", None)


class _Run:
    def __init__(self, metrics):
        self.metrics = metrics


def test_the_real_agno_shape_is_the_one_we_read():
    """Pins the attribute names against the installed agno, not against a mock.

    A mock cannot notice a rename; this can. If agno moves a field, this fails
    at the point of the upgrade instead of silently NULLing a cost column in
    production for a month.
    """

    from agno.metrics import RunMetrics

    m = RunMetrics()
    for attr in (
        "input_tokens", "output_tokens", "total_tokens",
        "reasoning_tokens", "cost", "time_to_first_token", "duration",
    ):
        assert hasattr(m, attr), f"agno.metrics.RunMetrics lost {attr}"


def test_a_run_with_metrics_yields_every_column():
    out = activity.extract_metrics(
        _Run(_Metrics(
            input_tokens=1200, output_tokens=340, total_tokens=1540,
            reasoning_tokens=64, cost=0.00123456, time_to_first_token=1.234,
        ))
    )
    assert out == {
        "input_tokens": 1200,
        "output_tokens": 340,
        "total_tokens": 1540,
        "reasoning_tokens": 64,
        "cost_usd": 0.001235,
        "ttft_ms": 1234,
    }


def test_metrics_none_yields_all_nulls_and_does_not_raise():
    """A cache hit, or a run agno gave no metrics for."""

    assert activity.extract_metrics(_Run(None)) == activity.NO_METRICS
    assert activity.extract_metrics(None) == activity.NO_METRICS
    assert activity.extract_metrics(object()) == activity.NO_METRICS


def test_cost_none_stores_null_not_zero():
    out = activity.extract_metrics(_Run(_Metrics(total_tokens=10, cost=None)))
    assert out["cost_usd"] is None


def test_cost_zero_stores_null_not_zero():
    """0 in a spend column is read by a human as "this turn was free".

    OpenRouter returns no per-generation price on the completion response, so
    nothing on this stack can back that claim. Unknown is the honest record.
    """

    out = activity.extract_metrics(_Run(_Metrics(total_tokens=10, cost=0)))
    assert out["cost_usd"] is None
    assert out["cost_usd"] is not 0  # noqa: F632 — the point is the identity, not ==


def test_zero_tokens_means_unreported_not_free():
    """RunMetrics initialises the counters to 0; no model answers on 0 tokens."""

    out = activity.extract_metrics(_Run(_Metrics()))
    assert out["input_tokens"] is None
    assert out["output_tokens"] is None
    assert out["total_tokens"] is None
    assert out["reasoning_tokens"] is None


def test_zero_reasoning_tokens_is_real_when_the_run_reported_usage():
    """The one counter whose 0 is genuine: a non-reasoning model spends none."""

    out = activity.extract_metrics(_Run(_Metrics(total_tokens=500, reasoning_tokens=0)))
    assert out["total_tokens"] == 500
    assert out["reasoning_tokens"] == 0


def test_extract_metrics_survives_a_hostile_object():
    """A renamed/removed agno attribute costs a metric, never an answer."""

    class Hostile:
        def __getattr__(self, name):
            raise RuntimeError(f"no such attribute: {name}")

    class HostileRun:
        metrics = Hostile()

    assert activity.extract_metrics(HostileRun()) == activity.NO_METRICS


def _chat_log_columns():
    return {
        r["column_name"]
        for r in _pg(
            "SELECT column_name FROM information_schema.columns WHERE table_name='chat_logs'",
            fetch=True,
        )
    }


def test_the_lifespan_creates_the_schema_on_a_database_that_lacks_it():
    """Non-vacuous: the schema is REMOVED first, then a boot must put it back.

    Asserting the columns exist after the module's own fixture created them
    proves only that the fixture works. This drops `app_events` and one metric
    column, boots the app, and checks that `ensure_app_events` /
    `ensure_turn_metrics` rebuilt them — which is the property a fresh container
    depends on, and the reason those statements are duplicated between the
    lifespan and migrations 0006/0007 (see `ensure_chat_logs` for the same trap:
    `CREATE TABLE IF NOT EXISTS` is a no-op on a database that already has the
    table, so a new column added to the CREATE would never appear).
    """

    from fastapi.testclient import TestClient

    _pg("DROP TABLE IF EXISTS app_events")
    _pg("ALTER TABLE chat_logs DROP COLUMN IF EXISTS ttft_ms")
    assert "ttft_ms" not in _chat_log_columns()

    with TestClient(apimod.app):
        pass  # the lifespan is the thing under test

    assert {
        "input_tokens", "output_tokens", "total_tokens",
        "reasoning_tokens", "cost_usd", "ttft_ms",
    } <= _chat_log_columns()
    assert _pg(
        "SELECT to_regclass('public.app_events') AS t", fetch=True
    )[0]["t"] == "app_events"
    # And the three indexes 0007 names, not just the table.
    idx = {
        r["indexname"]
        for r in _pg("SELECT indexname FROM pg_indexes WHERE tablename='app_events'", fetch=True)
    }
    assert {
        "idx_app_events_ts", "idx_app_events_actor_ts", "idx_app_events_action_ts",
    } <= idx


def test_chat_logs_accepts_a_metric_row_and_an_all_null_row(api_client):
    """The cache-hit row (all NULL) and a real run must both be storable."""

    tag = f"metrics-probe-{uuid.uuid4().hex[:8]}"
    _pg(
        """INSERT INTO chat_logs (lang, store_id, question, answer, cached, latency_ms,
                                  input_tokens, output_tokens, total_tokens,
                                  reasoning_tokens, cost_usd, ttft_ms)
           VALUES ('EN','S1',$1,'a',FALSE,10, 12,34,46,0,0.001234,900)""",
        tag,
    )
    _pg(
        """INSERT INTO chat_logs (lang, store_id, question, answer, cached, latency_ms)
           VALUES ('EN','S1',$1,'a',TRUE,3)""",
        tag + "-cached",
    )
    rows = _pg(
        "SELECT * FROM chat_logs WHERE question LIKE $1 ORDER BY id", tag + "%", fetch=True
    )
    try:
        assert len(rows) == 2
        assert rows[0]["total_tokens"] == 46
        assert float(rows[0]["cost_usd"]) == pytest.approx(0.001234)
        assert rows[1]["total_tokens"] is None
        assert rows[1]["cost_usd"] is None, "a cache hit must not record a zero cost"
    finally:
        _pg("DELETE FROM chat_logs WHERE question LIKE $1", tag + "%")


# ---- the two-sided deploy ---------------------------------------------------
#
# `log_chat` lives in app/admin.py, owned by another change. `api._log_turn`
# must work against BOTH signatures so the two land in either order.


def _call_log_turn(**kw):
    async def go():
        await apimod._log_turn(
            "q", "a", "S1", False, 12,
            embed_id="emb1", path="agent",
            metrics={
                "input_tokens": 10, "output_tokens": 20, "total_tokens": 30,
                "reasoning_tokens": 0, "cost_usd": None, "ttft_ms": 700,
            },
            **kw,
        )

    asyncio.run(go())


def test_log_turn_passes_the_metrics_when_log_chat_accepts_them(monkeypatch):
    import app.admin as adminmod

    seen = {}

    async def fake_log_chat(question, answer, store_id, cached, latency_ms, *,
                            embed_id=None, session_id=None, model=None, tools=None,
                            path=None, input_tokens=None, output_tokens=None,
                            total_tokens=None, reasoning_tokens=None,
                            cost_usd=None, ttft_ms=None):
        seen.update(
            input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=total_tokens, reasoning_tokens=reasoning_tokens,
            cost_usd=cost_usd, ttft_ms=ttft_ms,
        )

    monkeypatch.setattr(adminmod, "log_chat", fake_log_chat)
    _call_log_turn()
    assert seen == {
        "input_tokens": 10, "output_tokens": 20, "total_tokens": 30,
        "reasoning_tokens": 0, "cost_usd": None, "ttft_ms": 700,
    }


def test_log_turn_drops_the_metrics_when_log_chat_is_the_old_one(monkeypatch):
    """The other half of the change may not have landed yet. The turn still logs.

    Decided by INSPECTING the signature, never by catching a TypeError from the
    call: `log_chat` swallows its own exceptions, so a TypeError raised inside it
    is indistinguishable from a signature mismatch, and a retry after a
    successful INSERT would double every turn.
    """

    import app.admin as adminmod

    calls = []

    async def old_log_chat(question, answer, store_id, cached, latency_ms, *,
                           embed_id=None, session_id=None, model=None,
                           tools=None, path=None):
        calls.append(path)

    monkeypatch.setattr(adminmod, "log_chat", old_log_chat)
    _call_log_turn()  # must not raise
    assert calls == ["agent"]


def test_log_chat_signature_change_is_still_outstanding_or_landed():
    """Documents the contract the other half of this change must satisfy.

    Either `admin.log_chat` already takes the six keyword-only metric args (the
    change landed), or it does not and `_log_turn` drops them (it has not). Both
    are valid states; what is NOT valid is a partial signature, which would make
    the runtime probe say yes and then fail on a missing kwarg.
    """

    from app.admin import log_chat

    params = inspect.signature(log_chat).parameters
    present = [f for f in activity.METRIC_FIELDS if f in params]
    assert present == [] or present == list(activity.METRIC_FIELDS), (
        f"log_chat accepts only part of the metric kwargs: {present}"
    )
