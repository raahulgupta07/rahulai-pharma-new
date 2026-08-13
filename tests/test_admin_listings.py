"""Admin listing endpoints must work with NO filters applied.

`GET /admin/catalog` 500'd on its own default view. The handler builds its
WHERE clause from a list of conditions; with no `search` and no `category` that
list is empty, and an unguarded ``"WHERE " + " AND ".join([])`` emits::

    SELECT ... FROM catalog WHERE ORDER BY (brand_name = article_code), ...
                                  ^ syntax error at or near "ORDER"

Every sibling listing already guarded this (`if conds else ""`); catalog was
safe only because the stub filter always contributed a condition, and it went
unguarded the moment that filter was removed. Nothing caught it because the
suite only ever exercised `/admin/catalog/{code}` — the detail route — never
the list. So the tests below hit each listing with no query string at all,
which is exactly how the admin Data page opens it.

Needs live Postgres + Redis, like the rest of the suite.
"""

from __future__ import annotations

import pytest

from tests.test_admin_scope import _Admin

# Listings the admin SPA opens unfiltered. Each builds a dynamic WHERE, so each
# can regress the same way. A 500 here is the bug; 401/403/404 would be a
# different (and louder) problem, so they are asserted against too.
UNFILTERED_LISTINGS = [
    "/admin/catalog",
    "/admin/inventory",
    "/admin/conversations",
    "/admin/feedback",
]


@pytest.fixture
def admin():
    a = _Admin()
    yield a
    a.drop()


def test_catalog_lists_with_no_filters(api_client, admin):
    """The regression. Default view, no query string, must return rows."""

    r = api_client.get("/admin/catalog", headers=admin.headers)

    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


@pytest.mark.parametrize(
    "query",
    [
        "",  # the broken case
        "?search=&category=",  # the SPA sends empty strings, not omitted params
        "?limit=3",
        "?search=TESTOL",
        "?category=PRESCRIPTION",
        "?search=TESTOL&category=PRESCRIPTION",
        "?limit=3&offset=1",
    ],
)
def test_catalog_accepts_every_filter_combination(api_client, admin, query):
    """Both conditions optional means four WHERE shapes; all must be valid SQL.

    The empty-string variant matters on its own: the admin page sends
    `search=&category=` rather than omitting them, so it takes the same
    zero-condition path as no query string at all.
    """

    r = api_client.get(f"/admin/catalog{query}", headers=admin.headers)

    assert r.status_code == 200, f"{query} -> {r.status_code} {r.text[:200]}"
    assert isinstance(r.json(), list)


def test_catalog_honours_limit(api_client, admin):
    """Pins that the LIMIT/OFFSET placeholders still line up after the change.

    The parameter indexes are computed from `len(params)`, so a condition added
    or removed shifts them. Wrong indexes stay valid SQL and simply return the
    wrong page — no error to notice.
    """

    r = api_client.get("/admin/catalog?limit=3", headers=admin.headers)

    assert r.status_code == 200, r.text
    assert len(r.json()) <= 3


def test_catalog_flags_stub_rows(api_client, admin):
    """Stub rows are returned and flagged, never filtered out.

    Hiding them is what let a 100%-stub catalog on the customer host look like
    a clean table for weeks. Deleting the filter is also what exposed the WHERE
    bug above, so the two belong in one test file.
    """

    rows = api_client.get("/admin/catalog?limit=25", headers=admin.headers).json()

    assert rows, "catalog is empty; cannot assert on stub flagging"
    assert all("is_stub" in row for row in rows)


@pytest.mark.parametrize("path", UNFILTERED_LISTINGS)
def test_every_listing_survives_an_empty_query_string(api_client, admin, path):
    """The general form of the bug, across every dynamic-WHERE listing."""

    r = api_client.get(path, headers=admin.headers)

    assert r.status_code != 500, f"{path} 500'd unfiltered: {r.text[:300]}"
    assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"
