"""Admin-surface guards: store scoping, NULL stock, alias writes, CORS, credentials.

Five holes, one theme — the server was telling a pharmacy things that were not
true, or telling them to people who should not hear them:

* `GET /admin/catalog/{code}` returned EVERY branch's stock to every caller;
* it summed NULL (UNKNOWN) stock as 0, and a pharmacist reading "0" does not
  dispense;
* `drug_alias` had a reader and no writer, so the fast path's alias layer could
  never hit;
* an empty embed-credential store accepted every embed on the internet;
* CORS defaulted to `*`.

Needs live Postgres + Redis, like the rest of the suite.

⚠️ Every DB setup/teardown here goes through :func:`_pg`, a throwaway asyncpg
connection — NOT ``app.db.q``. The shared pool is bound to whichever loop created
it, and under ``api_client`` that is the TestClient's portal loop. An
``asyncio.run`` in the test body would hand that pool to a second loop and raise
"attached to a different loop". A private connection has no such affinity.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest import mock

import pytest

from app import auth as authmod
from app.config import Settings, get_settings

# Imported at collection time, not inside a test: app.api pulls in agno, which
# builds an asyncio.Lock() at import (see test_approval.py).
from app.api import cors_origins


def _pg(query: str, *args, fetch: bool = False):
    """Run one statement on a private connection. Never touches app.db's pool."""

    async def go():
        import asyncpg

        conn = await asyncpg.connect(get_settings().postgres_url)
        try:
            if fetch:
                return [dict(r) for r in await conn.fetch(query, *args)]
            await conn.execute(query, *args)
            return None
        finally:
            await conn.close()

    return asyncio.run(go())


def _ensure_schema():
    """The columns/tables these tests need, without importing the app's pool.

    Includes migrations/0001 (stock_qty/price NULLABLE). `app/schema.sql` has
    declared these columns nullable since defect A3, but the migration was only
    ever applied to the :8091 database — a dev DB created before it still carries
    `NOT NULL DEFAULT 0`, and would reject the very UNKNOWN rows these tests are
    about. The statements are no-ops once the constraint is gone.
    """

    _pg("ALTER TABLE inventory ALTER COLUMN stock_qty DROP NOT NULL")
    _pg("ALTER TABLE inventory ALTER COLUMN stock_qty DROP DEFAULT")
    _pg("ALTER TABLE inventory ALTER COLUMN price     DROP NOT NULL")
    _pg("ALTER TABLE inventory ALTER COLUMN price     DROP DEFAULT")
    _pg("ALTER TABLE users ADD COLUMN IF NOT EXISTS store_id TEXT")
    _pg(
        """CREATE TABLE IF NOT EXISTS drug_alias (
               alias        TEXT PRIMARY KEY,
               article_code TEXT NOT NULL REFERENCES catalog(article_code) ON DELETE CASCADE,
               source       TEXT,
               created_at   TIMESTAMPTZ DEFAULT now()
           )"""
    )


# ---- fixtures --------------------------------------------------------------


@pytest.fixture
def seeded_article():
    """One article stocked at three branches — one of them with UNKNOWN stock."""

    _ensure_schema()
    code = f"99{uuid.uuid4().int % 10**10:010d}"[:12]
    mine, sibling, unknown_site = "20005-CCYK", "20024-CC73", "20026-CC19"

    _pg(
        "INSERT INTO catalog (article_code, brand_name, generic_name) VALUES ($1,$2,$3)",
        code, "TESTOL 500MG", "Testolol",
    )
    _pg(
        """INSERT INTO inventory (article_code, site_code, stock_qty, price)
           VALUES ($1,$2,10,100),($1,$3,7,100),($1,$4,NULL,NULL)""",
        code, mine, sibling, unknown_site,
    )
    yield {"code": code, "mine": mine, "sibling": sibling, "unknown_site": unknown_site}
    _pg("DELETE FROM inventory WHERE article_code=$1", code)
    _pg("DELETE FROM drug_alias WHERE article_code=$1", code)
    _pg("DELETE FROM catalog WHERE article_code=$1", code)


class _Admin:
    """An approved admin account + a bearer header, optionally pinned to a branch."""

    def __init__(self, role="admin", store_id=None):
        _ensure_schema()
        self.email = f"scope-{uuid.uuid4().hex[:10]}@corp.mm"
        rows = _pg(
            """INSERT INTO users (email, name, role, auth_sources, active, approved, store_id)
               VALUES ($1,'Scoped',$2,ARRAY['local'],TRUE,TRUE,$3)
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


# ---- 1. embed credentials fail closed (API level) --------------------------


def test_session_create_rejects_unregistered_credentials(api_client):
    """The fail-closed check, through the real endpoint."""

    r = api_client.post(
        "/api/embed/session/create",
        json={"embed_id": "not-a-tenant", "public_key": "not-a-key"},
    )
    assert r.status_code == 403


def test_session_create_accepts_registered_credentials(api_client):
    """The suite's credential (conftest) still works — fail-closed, not fail-broken."""

    from tests.conftest import TEST_EMBED_ID, TEST_PUBLIC_KEY

    r = api_client.post(
        "/api/embed/session/create",
        json={"embed_id": TEST_EMBED_ID, "public_key": TEST_PUBLIC_KEY},
    )
    assert r.status_code == 200


# ---- 2. CORS ---------------------------------------------------------------


def test_cors_does_not_default_to_wildcard():
    assert "*" not in Settings.model_fields["allowed_origins"].default
    assert "*" not in cors_origins()


def test_cors_wildcard_only_by_explicit_opt_in():
    """`*` is honoured when written — and a BLANK setting falls back to the safe
    default rather than to `*`. The old `_origins or ["*"]` did the opposite, so
    the one value an operator is likeliest to leave behind while tightening the
    config was the value that undid the tightening."""

    from app import api

    base = get_settings().model_dump()

    with mock.patch(
        "app.api.get_settings", return_value=Settings(**{**base, "allowed_origins": "*"})
    ):
        assert api.cors_origins() == ["*"]

    with mock.patch(
        "app.api.get_settings", return_value=Settings(**{**base, "allowed_origins": "  "})
    ):
        assert "*" not in api.cors_origins()


# ---- 3. NULL stock is UNKNOWN, never zero ----------------------------------


def test_catalog_one_does_not_coerce_null_stock_to_zero(api_client, seeded_article, admin):
    """Unknown branches must not silently contribute 0 to the total."""

    r = api_client.get(f"/admin/catalog/{seeded_article['code']}", headers=admin.headers)
    assert r.status_code == 200
    body = r.json()

    # 10 + 7 + UNKNOWN. The old code answered 17 and called it "the" total; the
    # honest answer is 17 across the two branches we know, with one unknown.
    assert body["total_stock"] == 17
    assert body["site_count"] == 3
    assert body["known_site_count"] == 2
    assert body["unknown_site_count"] == 1

    by_site = {s["site_code"]: s["stock_qty"] for s in body["sites"]}
    assert by_site[seeded_article["unknown_site"]] is None      # not 0


def test_catalog_one_total_is_none_when_every_branch_is_unknown(api_client, admin):
    """All-NULL must read as UNKNOWN (None), not as zero-on-hand."""

    _ensure_schema()
    code = f"98{uuid.uuid4().int % 10**10:010d}"[:12]
    _pg("INSERT INTO catalog (article_code, brand_name) VALUES ($1,'NULLOL')", code)
    _pg(
        """INSERT INTO inventory (article_code, site_code, stock_qty)
           VALUES ($1,'20005-CCYK',NULL),($1,'20024-CC73',NULL)""",
        code,
    )
    try:
        r = api_client.get(f"/admin/catalog/{code}", headers=admin.headers)
        assert r.status_code == 200
        body = r.json()
        assert body["total_stock"] is None          # UNKNOWN — and never 0
        assert body["known_site_count"] == 0
        assert body["unknown_site_count"] == 2
    finally:
        _pg("DELETE FROM inventory WHERE article_code=$1", code)
        _pg("DELETE FROM catalog WHERE article_code=$1", code)


# ---- 4. store scoping ------------------------------------------------------


def test_catalog_one_scopes_a_pinned_admin_to_their_own_branch(api_client, seeded_article):
    """A branch-pinned account must not see a sibling branch's stock."""

    a = _Admin(store_id=seeded_article["mine"])
    try:
        r = api_client.get(f"/admin/catalog/{seeded_article['code']}", headers=a.headers)
        assert r.status_code == 200
        body = r.json()

        sites = [s["site_code"] for s in body["sites"]]
        assert sites == [seeded_article["mine"]]
        assert seeded_article["sibling"] not in sites
        assert body["store_scope"] == seeded_article["mine"]
        assert body["total_stock"] == 10           # own branch only, not 17
        assert body["site_count"] == 1
    finally:
        a.drop()


@pytest.mark.parametrize("token_form", ["20005", "CCYK", "20005-CCYK"])
def test_catalog_one_scope_accepts_prefix_and_suffix_site_tokens(
    api_client, seeded_article, token_form
):
    """Scope matching goes through tools._site_clause, so the full code, its
    numeric prefix and its alpha suffix all resolve to the SAME single branch —
    and a prefix never substring-matches a sibling (the leak class already fixed
    in the tools layer)."""

    a = _Admin(store_id=token_form)
    try:
        r = api_client.get(f"/admin/catalog/{seeded_article['code']}", headers=a.headers)
        assert r.status_code == 200
        sites = [s["site_code"] for s in r.json()["sites"]]
        assert sites == [seeded_article["mine"]]
    finally:
        a.drop()


def test_catalog_one_unscoped_admin_keeps_the_full_view(api_client, seeded_article, admin):
    r = api_client.get(f"/admin/catalog/{seeded_article['code']}", headers=admin.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["store_scope"] is None
    assert body["site_count"] == 3


def test_super_admin_is_never_scoped(api_client, seeded_article):
    """A pinned super_admin still sees everything — pinning must not become a way
    to lock the top account out of its own data."""

    a = _Admin(role="super_admin", store_id=seeded_article["mine"])
    try:
        r = api_client.get(f"/admin/catalog/{seeded_article['code']}", headers=a.headers)
        assert r.status_code == 200
        body = r.json()
        assert body["store_scope"] is None
        assert body["site_count"] == 3
    finally:
        a.drop()


def test_scope_is_server_side_and_cannot_be_set_by_the_client(api_client, seeded_article):
    """The scope comes from the users row, never from the request. A pinned caller
    who ASKS for a sibling branch still gets only their own."""

    a = _Admin(store_id=seeded_article["mine"])
    try:
        r = api_client.get(
            f"/admin/catalog/{seeded_article['code']}",
            params={"scope": seeded_article["sibling"], "store_id": seeded_article["sibling"]},
            headers=a.headers,
        )
        assert r.status_code == 200
        assert [s["site_code"] for s in r.json()["sites"]] == [seeded_article["mine"]]
    finally:
        a.drop()


def test_users_patch_sets_and_clears_the_store_pin(api_client, admin):
    """The pin is assignable server-side, so the feature is reachable without SQL."""

    target = _Admin()
    try:
        r = api_client.patch(
            f"/admin/users/{target.id}", json={"store_id": "20005-CCYK"}, headers=admin.headers
        )
        assert r.status_code == 200 and r.json()["store_id"] == "20005-CCYK"

        listed = {u["email"]: u for u in api_client.get("/admin/users", headers=admin.headers).json()}
        assert listed[target.email]["store_id"] == "20005-CCYK"

        # "" clears the pin back to the global view (distinct from None = unchanged).
        r = api_client.patch(
            f"/admin/users/{target.id}", json={"store_id": ""}, headers=admin.headers
        )
        assert r.status_code == 200 and r.json()["store_id"] is None
    finally:
        target.drop()


# ---- 5. drug_alias write path ----------------------------------------------


def test_alias_crud_round_trip(api_client, seeded_article, admin):
    code = seeded_article["code"]

    # create — mixed case + padding must normalise to the stored key
    r = api_client.post(
        "/admin/aliases",
        json={"alias": "  TeStOl  ", "article_code": code, "source": "pharmacist"},
        headers=admin.headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["alias"] == "testol"
    assert r.json()["article_code"] == code

    # list
    rows = api_client.get(
        "/admin/aliases", params={"article_code": code}, headers=admin.headers
    ).json()
    assert [x["alias"] for x in rows] == ["testol"]
    assert rows[0]["brand_name"] == "TESTOL 500MG"
    assert rows[0]["source"] == "pharmacist"

    # delete
    r = api_client.delete("/admin/aliases/testol", headers=admin.headers)
    assert r.status_code == 200 and r.json()["removed"] == 1
    assert api_client.get(
        "/admin/aliases", params={"article_code": code}, headers=admin.headers
    ).json() == []


def test_resolver_hits_an_admin_written_alias(seeded_article):
    """The real consumer reads what the write path wrote.

    A CRUD test that only round-trips through its own endpoints would still pass
    if the alias were stored under a key `resolver._alias_lookup` can never find
    (it queries `WHERE alias = lower($1)` against a merely-stripped mention). So
    drive the actual writer, then the actual reader.

    No api_client here on purpose: this needs the app's own pool, on one loop.
    """

    from app import resolver
    from app.admin import Alias, add_alias, del_alias, ensure_admin_schema
    from app.db import close_pool

    async def go():
        try:
            await ensure_admin_schema()
            await add_alias(
                Alias(alias="  TeStOl  ", article_code=seeded_article["code"], source="pharmacist")
            )
            # The mention as a user would type it — resolve() only strips it.
            hit = await resolver.resolve("Testol")
            miss_before_alias = await resolver.resolve("zzz-no-such-drug-zzz")
            await del_alias("testol")
            gone = await resolver.resolve("Testol")
            return hit, miss_before_alias, gone
        finally:
            await close_pool()

    hit, miss, gone = asyncio.run(go())

    assert hit.status is resolver.Resolution.RESOLVED
    assert hit.source == "alias"                     # the layer that was always a miss
    assert hit.article_code == seeded_article["code"]

    assert miss.status is resolver.Resolution.NOT_FOUND

    # Deleting the alias drops resolution back to the trigram layer (or nothing);
    # either way it is no longer served from the alias table.
    assert gone.source != "alias"


def test_alias_upsert_retargets_instead_of_duplicating(api_client, seeded_article, admin):
    code = seeded_article["code"]
    other = f"97{uuid.uuid4().int % 10**10:010d}"[:12]
    _pg("INSERT INTO catalog (article_code, brand_name) VALUES ($1,'OTHEROL')", other)
    try:
        api_client.post(
            "/admin/aliases", json={"alias": "dupe", "article_code": code}, headers=admin.headers
        )
        r = api_client.post(
            "/admin/aliases", json={"alias": "dupe", "article_code": other}, headers=admin.headers
        )
        assert r.status_code == 200
        assert r.json()["article_code"] == other

        rows = api_client.get(
            "/admin/aliases", params={"search": "dupe"}, headers=admin.headers
        ).json()
        assert len(rows) == 1 and rows[0]["article_code"] == other
    finally:
        _pg("DELETE FROM drug_alias WHERE article_code=$1", other)
        _pg("DELETE FROM catalog WHERE article_code=$1", other)


def test_alias_rejects_unknown_article(api_client, admin):
    """drug_alias FKs to catalog — an unknown code must 404, not 500 from asyncpg."""

    r = api_client.post(
        "/admin/aliases",
        json={"alias": "ghost", "article_code": "000000000000"},
        headers=admin.headers,
    )
    assert r.status_code == 404


def test_alias_rejects_a_too_short_alias(api_client, seeded_article, admin):
    r = api_client.post(
        "/admin/aliases",
        json={"alias": " x ", "article_code": seeded_article["code"]},
        headers=admin.headers,
    )
    assert r.status_code == 400


def test_alias_write_bumps_data_version(api_client, seeded_article, admin):
    """An alias changes what a question RESOLVES to, so answers cached against the
    old resolution are now wrong for the same words. Same rule as
    /admin/graph/rebuild: a writer that changes answers bumps the version."""

    created = api_client.post(
        "/admin/aliases",
        json={"alias": "bumpme", "article_code": seeded_article["code"]},
        headers=admin.headers,
    )
    assert created.status_code == 200
    v1 = created.json()["data_version"]

    deleted = api_client.delete("/admin/aliases/bumpme", headers=admin.headers)
    assert deleted.status_code == 200
    assert deleted.json()["data_version"] > v1
