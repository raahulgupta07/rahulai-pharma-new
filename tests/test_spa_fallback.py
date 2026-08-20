"""The /admin boundary: SPA deep links serve the shell, API misses serve JSON.

Both halves used to be wrong in the same place. The static mount fell back to
index.html for ANY extensionless miss, so `GET /admin/nonexistent-xyz` and a
traversal attempt that failed to match the SFTP download route both answered
**200 text/html** — nothing leaked, but a scanner reading status codes flags it
and a mistyped API call looks like a working page. Meanwhile a cold reload of
/admin/users hit the API route of the same name and rendered raw JSON.

These run without the lifespan (plain TestClient, no context manager): none of
them touch Postgres or Redis, and the point is the routing decision.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import app

# What a browser sends when the user types the URL or hits reload. Both headers
# are asserted separately below, because the fallback (no Sec-Fetch-*) is what
# an older browser looks like.
NAV_HEADERS = {
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# What the admin SPA's own fetch wrapper sends — and curl, and a scanner.
API_HEADERS = {"sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "accept": "*/*"}


def _spa_routes() -> list[str]:
    """Every client route, read from admin/src/routes/ rather than hardcoded.

    Hardcoding two of them is how this regression hides: the collision is with
    whichever page happens to share a name with an API route, and that set grows
    every time someone adds an endpoint.
    """

    root = Path(__file__).parent.parent / "admin" / "src" / "routes"
    routes = []
    for page in sorted(root.rglob("+page.svelte")):
        rel = page.parent.relative_to(root).as_posix()
        routes.append("/admin" if rel == "." else f"/admin/{rel}")
    return routes


def _client() -> TestClient:
    return TestClient(app)


def _build_dir() -> Path:
    return Path(__file__).parent.parent / "admin" / "build"


def _has_build() -> bool:
    return (_build_dir() / "index.html").is_file()


needs_build = pytest.mark.skipif(
    not _has_build(), reason="admin SPA not built (npm run build)"
)


def test_spa_routes_are_not_empty():
    """A silent zero here would make every deep-link test vacuous."""

    assert len(_spa_routes()) >= 10


# ---- deep links: the regression that matters most ---------------------------


@needs_build
@pytest.mark.parametrize("route", _spa_routes())
def test_deep_link_serves_the_shell(route):
    r = _client().get(route, headers=NAV_HEADERS)
    assert r.status_code == 200, route
    assert r.headers["content-type"].startswith("text/html"), route
    assert "<html" in r.text.lower(), route


@needs_build
@pytest.mark.parametrize("route", ["/admin/users", "/admin/stores", "/admin/graph",
                                   "/admin/conversations", "/admin/learning"])
def test_deep_link_wins_over_a_same_named_api_route(route):
    """These five paths are BOTH a page and a GET endpoint.

    The API route is registered before the mount (it must be — the SPA's fetch
    calls have to win), so a reload of the page used to render require_admin's
    401 JSON in the browser.
    """

    r = _client().get(route, headers=NAV_HEADERS)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")


@needs_build
def test_deep_link_without_sec_fetch_headers_still_works():
    """An older browser sends no Sec-Fetch-*; Accept alone must carry it."""

    r = _client().get("/admin/ftp", headers={"accept": "text/html,*/*;q=0.8"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")


@needs_build
def test_unknown_page_path_from_a_browser_still_gets_the_shell():
    """A typo'd page is indistinguishable from a real one; the router says 404."""

    r = _client().get("/admin/no-such-page", headers=NAV_HEADERS)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")


# ---- API misses: JSON 404, never the shell ----------------------------------


@pytest.mark.parametrize("path", [
    "/admin/nonexistent-xyz",
    "/admin/does/not/exist",
    "/admin/sftp/file/x/y",                       # traversal shape, unmatched route
    "/admin/sftp/file/a/../../../etc/passwd",
    "/admin/users/extra/segments",
])
def test_api_miss_is_json_404(path):
    r = _client().get(path, headers=API_HEADERS)
    assert r.status_code == 404, (path, r.status_code)
    assert r.headers["content-type"].startswith("application/json"), path
    assert "<html" not in r.text.lower(), path


def test_api_miss_with_no_headers_at_all_is_json_404():
    """curl sends `Accept: */*` and no Sec-Fetch-*: still a data request."""

    r = _client().get("/admin/nonexistent-xyz")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


@needs_build
def test_a_missing_asset_never_returns_the_shell():
    """A 200 text/html for a .js request breaks in confusing ways.

    True for a browser navigation too: the extension is the signal, not the
    caller — `<script src>` is `Sec-Fetch-Dest: script`, but someone opening the
    URL in a tab must not be told the file exists.
    """

    for headers in (API_HEADERS, NAV_HEADERS, {"sec-fetch-dest": "script"}):
        for path in ("/admin/_app/immutable/nope.js",
                     "/admin/missing.css",
                     "/admin/nope.svg"):
            r = _client().get(path, headers=headers)
            assert r.status_code == 404, (path, headers)
            assert "<html" not in r.text.lower(), (path, headers)


@needs_build
def test_real_assets_still_serve():
    """The guard above must not be achieved by 404ing every asset."""

    r = _client().get("/admin/favicon.svg", headers={"sec-fetch-dest": "image"})
    assert r.status_code == 200


@needs_build
def test_admin_root_serves_the_shell_either_way():
    for headers in (NAV_HEADERS, API_HEADERS):
        r = _client().get("/admin/", headers=headers)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")


@needs_build
def test_the_shell_is_never_served_from_cache_without_revalidating():
    """A deploy that the user cannot see is a deploy that did not happen.

    The shell is the only file whose contents change while its URL stays the
    same, and it names which fingerprinted bundle to load. StaticFiles sends an
    ETag but no Cache-Control, which lets a browser apply heuristic freshness
    and keep serving the PREVIOUS console for hours after a release. Both paths
    that can produce the shell are asserted, because they are different code:
    the deep-link middleware and the static mount.
    """

    for path, headers in (("/admin/", NAV_HEADERS),      # static mount, html=True
                          ("/admin/", API_HEADERS),
                          ("/admin/users", NAV_HEADERS),  # deep-link middleware
                          ("/admin/settings", NAV_HEADERS)):
        r = _client().get(path, headers=headers)
        assert r.status_code == 200, (path, headers)
        assert r.headers["content-type"].startswith("text/html"), (path, headers)
        assert r.headers.get("cache-control") == "no-cache", (
            path, headers, r.headers.get("cache-control"))


@needs_build
def test_fingerprinted_assets_are_cached_hard():
    """The other half: these must NOT revalidate, or every load pays for it.

    Their URL changes whenever their contents do, so a year is safe. Picking a
    real file from the build rather than a hardcoded name — the hashes change
    on every build.
    """

    build = _build_dir()
    immutable = build / "_app" / "immutable"
    assets = [f for f in immutable.rglob("*.js")] + [f for f in immutable.rglob("*.css")]
    assert assets, "no fingerprinted assets in the build — the guard would pass vacuously"
    rel = assets[0].relative_to(build)
    r = _client().get(f"/admin/{rel.as_posix()}", headers={"sec-fetch-dest": "script"})
    assert r.status_code == 200, rel
    assert "immutable" in r.headers.get("cache-control", ""), r.headers.get("cache-control")


def test_non_get_is_never_diverted_to_the_shell():
    """A POST is never a navigation; diverting one would swallow an API call."""

    r = _client().post("/admin/upload", headers=NAV_HEADERS)
    assert r.status_code != 200
    assert "<html" not in r.text.lower()
