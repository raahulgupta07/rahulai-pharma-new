"""`POST /admin/embed/preflight` — the checklist must MEASURE, never assume.

The Embed test page shows an operator three ticks before an outlet goes live.
The dangerous one is "this store cannot read another branch's stock": that is the
leak class CLAUDE.md records shipping twice, and a green tick that was never
computed is worse than a blank row, because it ends the investigation.

So the tests here pin the three ways the endpoint could lie:

* it could pass by matching sites as SUBSTRINGS — a `200059-CCZZ` decoy branch
  contains the scope token `20005`, so a substring matcher reports two branches
  visible where an anchored one reports one. This is the same decoy technique as
  `tests/test_tools_scope.py`, and for the same reason: every real site code the
  fixtures use is substring-DISJOINT from the tokens, so without a decoy the
  tests cannot tell a correct matcher from the documented-bad one;
* it could pass a store it never read — an empty store must answer `ok: None`
  (UNKNOWN, grey in the UI), never `True`;
* it could be readable by an account that should not see other branches' data,
  so a plain `admin` must get 403.

Needs live Postgres + Redis. DB setup/teardown goes through a private asyncpg
connection (`_pg`) for the loop-affinity reason documented at the top of
`tests/test_admin_scope.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest import mock

import pytest

from app import auth as authmod
from app.config import Settings, get_settings

from tests.pgconn import pg

MINE = "20005-CCYK"
SIBLING = "20024-CC73"
# Contains "20005" as a substring; is NOT its anchored match. A substring matcher
# counts it as visible under scope "20005"; `_site_clause` does not.
DECOY = "200059-CCZZ"


def _pg(query: str, *args, fetch: bool = False):
    """Run one statement on a private connection. Never touches app.db's pool.

    One connection per PROCESS, not per statement — see tests/pgconn.py for why
    the previous arrangement was the suite's whole wall clock.
    """

    return pg(query, *args, fetch=fetch)


def _purge_decoy():
    """Drop every decoy row whatever article it hangs off.

    Seeding purges FIRST as well as last: a killed run never reaches its
    teardown, and a stranded decoy makes the NEXT run fail somewhere else, where
    it reads as a live scope leak rather than as test litter.
    """

    _pg("DELETE FROM inventory WHERE site_code=$1", DECOY)


@pytest.fixture
def preflight_article():
    """One article at my branch and at a real sibling, plus a substring decoy.

    The decoy stocks the SAME article, so a substring matcher sees it both in
    `list_sites` and in a `get_stock` probe.
    """

    _purge_decoy()
    code = f"95{uuid.uuid4().int % 10**10:010d}"[:12]
    _pg(
        "INSERT INTO catalog (article_code, brand_name, generic_name) VALUES ($1,$2,$3)",
        code, "PREFLIGHTOL 100MG", "Preflightolol",
    )
    _pg(
        """INSERT INTO inventory (article_code, site_code, stock_qty, price)
           VALUES ($1,$2,10,100),($1,$3,7,100),($1,$4,999,100)""",
        code, MINE, SIBLING, DECOY,
    )
    yield code
    _pg("DELETE FROM inventory WHERE article_code=$1", code)
    _purge_decoy()
    _pg("DELETE FROM catalog WHERE article_code=$1", code)


class _Admin:
    """An approved account + a bearer header. `role` decides the 403 case."""

    def __init__(self, role: str = "super_admin"):
        self.email = f"preflight-{uuid.uuid4().hex[:10]}@corp.mm"
        rows = _pg(
            """INSERT INTO users (email, name, role, auth_sources, active, approved)
               VALUES ($1,'Preflight',$2,ARRAY['local'],TRUE,TRUE)
               RETURNING id, email, role""",
            self.email, role, fetch=True,
        )
        self.id = rows[0]["id"]
        self.token = authmod.make_token(rows[0])["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def drop(self):
        _pg("DELETE FROM users WHERE id=$1", self.id)


@pytest.fixture
def super_admin():
    a = _Admin("super_admin")
    yield a
    a.drop()


@pytest.fixture
def plain_admin():
    a = _Admin("admin")
    yield a
    a.drop()


def _post(client, headers, store_id: str, embed_id: str = None, public_key: str = None):
    from tests.conftest import TEST_EMBED_ID, TEST_PUBLIC_KEY

    return client.post(
        "/admin/embed/preflight",
        headers=headers,
        json={
            "store_id": store_id,
            "embed_id": TEST_EMBED_ID if embed_id is None else embed_id,
            "public_key": TEST_PUBLIC_KEY if public_key is None else public_key,
        },
    )


# ---- happy path -------------------------------------------------------------


def test_scoped_store_reports_one_branch_and_no_sibling_leak(
    api_client, preflight_article, super_admin
):
    """The whole point: every field is a fact the endpoint computed."""

    r = _post(api_client, super_admin.headers, MINE)
    assert r.status_code == 200
    body = r.json()

    assert body["credential"]["ok"] is True
    scope = body["scope"]
    assert scope["ok"] is True, scope["detail"]
    assert scope["sites_visible"] == 1
    assert scope["sibling_leaked"] is False
    assert scope["rows_checked"] > 0
    assert MINE in scope["detail"]

    assert set(body["cors"]) == {"wildcard", "count", "origins"}
    assert body["cors"]["count"] == len(body["cors"]["origins"])


@pytest.mark.parametrize("token_form", ["20005", "CCYK", "20005-CCYK"])
def test_prefix_and_suffix_tokens_resolve_to_the_same_single_branch(
    api_client, preflight_article, super_admin, token_form
):
    """A store_id may be the full code, its numeric prefix or its alpha suffix —
    all three are the same one branch, exactly as `_site_clause` reads them."""

    body = _post(api_client, super_admin.headers, token_form).json()
    assert body["scope"]["sites_visible"] == 1
    assert body["scope"]["ok"] is True, body["scope"]["detail"]


# ---- the anchored-matching pin ---------------------------------------------


def test_a_substring_decoy_branch_is_not_counted_as_visible(
    api_client, preflight_article, super_admin
):
    """`200059-CCZZ` is a DIFFERENT branch that merely contains the token `20005`.

    Anchored (`tools._site_clause`), the scope sees one branch. Substring-matched
    — the documented-bad `ILIKE '%'||$n||'%'` — it sees two, and the extra one is
    another store's stock. Verified by deliberately swapping the matcher for a
    substring one: this test fails, the others stay green. That asymmetry is the
    whole reason the decoy is seeded.
    """

    body = _post(api_client, super_admin.headers, "20005").json()
    scope = body["scope"]

    assert scope["sites_visible"] == 1, scope["detail"]
    assert DECOY not in scope["detail"]
    assert scope["ok"] is True, scope["detail"]


# ---- credential -------------------------------------------------------------


def test_unregistered_credential_is_reported_false(
    api_client, preflight_article, super_admin
):
    """Fail-closed, and said out loud — not a 400 the UI has to interpret."""

    r = _post(
        api_client, super_admin.headers, MINE,
        embed_id="not-a-tenant", public_key="not-a-key",
    )
    assert r.status_code == 200
    assert r.json()["credential"]["ok"] is False
    # the scope check still runs: one bad row must not blank the others
    assert r.json()["scope"]["ok"] is True


# ---- authorisation ----------------------------------------------------------


def test_plain_admin_is_refused(api_client, plain_admin):
    """super_admin only, like every other /admin/embed/* endpoint."""

    r = _post(api_client, plain_admin.headers, MINE)
    assert r.status_code == 403


# ---- UNKNOWN is not a pass --------------------------------------------------


def test_store_with_no_inventory_is_unknown_not_true(api_client, super_admin):
    """Nothing to read means nothing was proved. `None`, never `True`."""

    r = _post(api_client, super_admin.headers, "99999-NOSUCHBRANCH")
    assert r.status_code == 200
    scope = r.json()["scope"]
    assert scope["ok"] is None
    assert scope["sites_visible"] == 0
    assert scope["sibling_leaked"] is None
    assert "no branch" in scope["detail"]


# ---- CORS -------------------------------------------------------------------


def test_cors_wildcard_is_reported(api_client, super_admin):
    """`*` in the effective allowlist is a fact the operator must see."""

    from app import api as apimod

    base = get_settings().model_dump()
    with mock.patch(
        "app.api.get_settings", return_value=Settings(**{**base, "allowed_origins": "*"})
    ):
        assert apimod.cors_origins() == ["*"]      # the precondition, not an assumption
        body = _post(api_client, super_admin.headers, MINE).json()

    assert body["cors"]["wildcard"] is True
    assert "*" in body["cors"]["origins"]


def test_cors_is_not_wildcard_by_default(api_client, super_admin):
    body = _post(api_client, super_admin.headers, MINE).json()
    assert body["cors"]["wildcard"] is False
