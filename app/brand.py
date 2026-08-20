"""White-label branding: the text and the logos an operator can change.

Everything here exists so a deployment can be renamed without a code change —
product name, the sentences the login screen shows, and four image assets. Two
decisions shape the whole module:

**Storage is Postgres, never the filesystem.** ``docker-compose.yml`` mounts no
source volume for ``api``; both ``app/`` and the built SPA are COPYed into the
image (see CLAUDE.md, "Deploy gotcha"). A logo written to a container path is
therefore destroyed by the next ``docker compose build`` — which is every
deploy. The operator would upload a logo, see it work, and lose it at the next
release with nothing in the logs to explain it. Bytes live in ``brand_assets``.

**Every field falls back to the string the UI hardcodes today.** An unconfigured
install must render exactly as it did before this table existed, so the defaults
below are not placeholders: they are the literal strings that were in the
Svelte source. A missing row, an empty JSONB blob and a hand-truncated column
all resolve to the same rendered page.

The schema is created two ways — ``migrations/0005_branding.sql`` for a database
someone already has, and :func:`ensure_brand_tables` on every boot for a fresh
one. That mirrors ``ensure_auth_events`` / ``ensure_chat_logs``: the two paths
have to converge on one schema, and a fresh container must not need a human to
run psql before the login screen works.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from app.db import execute, q

# ---------------------------------------------------------------------------
# Defaults — the strings the UI hardcoded before branding existed
# ---------------------------------------------------------------------------

DEFAULTS: Dict[str, str] = {
    "product_name": "City Care Agent",
    "short_name": "City Care",
    "tagline": "Stock Intelligence",
    "console_subtitle": "Admin console",
    "login_promise": (
        "Ask about stock in plain words — English or Burmese. "
        "Read-only by design."
    ),
    "legal_footer": "© 2026 City Medical Health & Logistics · Read-only",
    "pending_title": "CMHL Secure Platform",
    "parent_name": "CMHL",
    "dark_logo_mode": "chip",
}

TEXT_FIELDS: Tuple[str, ...] = tuple(DEFAULTS)

#: Per-field length caps. The two name fields are tighter than the rest because
#: they render inside fixed chrome — the sidebar chip and the collapsed rail —
#: where an over-long string does not wrap, it overlaps the nav.
MAX_LEN: Dict[str, int] = {"product_name": 24, "short_name": 12}
DEFAULT_MAX_LEN = 200

#: ``dark_logo_mode`` is not free text: it selects a rendering branch. ``chip``
#: puts the light lockup on an accent chip; ``variant`` swaps in the separately
#: uploaded dark lockup. Anything else is a branch that does not exist.
DARK_LOGO_MODES: Tuple[str, ...] = ("chip", "variant")

ASSET_KEYS: Tuple[str, ...] = ("icon", "lockup", "lockup_dark", "parent")

#: Accepted upload types. **SVG is deliberately absent and must stay absent** —
#: an SVG is XML that may carry ``<script>``, and these files are served
#: same-origin from the host that also serves the admin SPA, whose bearer token
#: lives in ``localStorage``. Storing one would be stored XSS against a
#: super_admin. Raster only.
ALLOWED_MIME: Dict[str, str] = {"image/png": "png", "image/jpeg": "jpg"}

MAX_ASSET_BYTES = 1024 * 1024          # 1 MB
MAX_ASSET_PIXELS = 1024                # per side


class BrandError(ValueError):
    """A rejected branding write. Callers map this to an HTTP 400."""


class BrandTooLarge(BrandError):
    """An upload past the byte cap. Mapped to 413 so the UI can say why."""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


async def ensure_brand_tables() -> None:
    """Create the branding tables. Idempotent; runs at every boot.

    Same statements as ``migrations/0005_branding.sql``. See the module
    docstring for why both exist.
    """

    await execute(
        """
        CREATE TABLE IF NOT EXISTS brand_config (
            id         INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            data       JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    await execute(
        """
        CREATE TABLE IF NOT EXISTS brand_assets (
            key        TEXT PRIMARY KEY,
            mime       TEXT NOT NULL,
            bytes      BYTEA NOT NULL,
            sha256     TEXT NOT NULL,
            width      INT,
            height     INT,
            size_bytes INT,
            updated_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )


# ---------------------------------------------------------------------------
# Text validation
# ---------------------------------------------------------------------------


def _strip_controls(text: str) -> str:
    """Drop control/format characters, then trim.

    These strings are injected into headings, `<title>` and a PDF-less login
    screen. A stray newline splits a heading; a bidi override (U+202E, category
    Cf) reverses the rendered product name without changing a single visible
    character in the admin textbox — the operator sees one thing and every user
    sees another. Neither has a legitimate use in a 24-character product name,
    so both classes go.
    """

    return "".join(
        ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf")
    ).strip()


def clean_text(field: str, value: Any) -> str:
    """Validate one branding field, returning the value that should be stored.

    Raises :class:`BrandError` on a non-string, an over-length string, a string
    that is empty once control characters are removed, or a ``dark_logo_mode``
    outside :data:`DARK_LOGO_MODES`.
    """

    if field not in DEFAULTS:
        raise BrandError(f"unknown branding field {field!r}")
    # `isinstance(True, int)` is the reason this is not a truthiness test: a
    # JSON `true` or a number must be refused outright, not stringified into a
    # product name that reads "True".
    if not isinstance(value, str):
        raise BrandError(f"{field} must be a string")

    cleaned = _strip_controls(value)
    if not cleaned:
        raise BrandError(f"{field} must not be empty")

    if field == "dark_logo_mode":
        if cleaned not in DARK_LOGO_MODES:
            raise BrandError(
                f"dark_logo_mode must be one of {' or '.join(DARK_LOGO_MODES)}"
            )
        return cleaned

    cap = MAX_LEN.get(field, DEFAULT_MAX_LEN)
    if len(cleaned) > cap:
        raise BrandError(f"{field} must be at most {cap} characters")
    return cleaned


def validate_updates(updates: Any) -> Dict[str, str]:
    """Validate a partial text update. Returns only the fields it accepted."""

    if not isinstance(updates, dict):
        raise BrandError("body must be an object")
    unknown = [k for k in updates if k not in DEFAULTS]
    if unknown:
        # Accepting an unknown key silently would report success and change
        # nothing — the failure mode `set_ingest_config` and `/security-config`
        # both go out of their way to avoid.
        raise BrandError(f"unknown branding field {unknown[0]!r}")
    if not updates:
        raise BrandError("nothing to update")
    return {k: clean_text(k, v) for k, v in updates.items()}


def _stored_ok(field: str, value: Any) -> bool:
    """Would this stored value survive validation? (drives `is_default`)"""

    try:
        clean_text(field, value)
        return True
    except BrandError:
        return False


def _coerce_stored(raw: Any) -> Dict[str, str]:
    """Resolve a stored blob to a full, safe field set — defaults for the rest.

    Validation on write is not enough. The row is reachable by psql, by a
    restore from a dump, and by whatever the next migration does to it, and
    ``dark_logo_mode`` selects a rendering branch: an unknown value there is a
    branch that does not exist, so it is clamped back to the default on READ as
    well as refused on write. Anything non-string, over-length or unrecognised
    is dropped rather than repaired — a wrong-but-plausible product name is
    worse than the default one.
    """

    out = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return out
    for field in TEXT_FIELDS:
        if field not in raw:
            continue
        try:
            out[field] = clean_text(field, raw[field])
        except BrandError:
            continue  # keep the default
    return out


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

# One statement, no in-process cache, on purpose. `/brand` is on the hot path of
# every login-screen render, but the query is a single-row PK lookup plus at
# most four metadata rows and no BYTEA — cheaper than the cache-coherence
# problem a cache would create. The API runs multiple uvicorn workers in
# production, and an in-process copy invalidated by the writing worker stays
# stale in every OTHER worker until it happens to be restarted: half the fleet
# would serve the old logo URL indefinitely, which looks exactly like a browser
# cache bug and is unfixable from the console. The `bytes` column is excluded
# here so this stays small no matter how many assets are set.
_BRAND_QUERY = """
    SELECT
      (SELECT data::text       FROM brand_config WHERE id = 1) AS data,
      (SELECT updated_at::text FROM brand_config WHERE id = 1) AS config_updated_at,
      COALESCE((
        SELECT json_agg(json_build_object(
                 'key', key, 'mime', mime, 'sha256', sha256,
                 'width', width, 'height', height,
                 'size_bytes', size_bytes, 'updated_at', updated_at::text
               ) ORDER BY key)
          FROM brand_assets
      ), '[]'::json)::text AS assets
"""


def _loads(value: Any) -> Any:
    """asyncpg hands JSONB back as text unless a codec is registered."""

    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


def _version(config: Dict[str, str], assets: Dict[str, Dict]) -> str:
    """One 8-char stamp covering the text AND every asset's content hash.

    The SPA cache-busts its whole brand surface with a single value, so the
    stamp has to move when *anything* moves — replacing only the logo must
    change it, or the login screen keeps painting the old one.
    """

    material = json.dumps(
        {
            "config": {k: config[k] for k in TEXT_FIELDS},
            "assets": {k: assets[k]["sha256"] for k in sorted(assets)},
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]


def _asset_urls(assets: Dict[str, Dict]) -> Dict[str, Optional[str]]:
    """Content-addressed URLs — the reason the asset route may cache forever."""

    return {
        key: (
            f"/brand/asset/{key}?v={assets[key]['sha256']}" if key in assets else None
        )
        for key in ASSET_KEYS
    }


def default_document() -> Dict[str, Any]:
    """The document an unconfigured install (or a database blip) renders.

    ``version`` is the literal ``"default"`` rather than a hash, so a client
    holding a stamped copy can tell "nothing is configured" apart from "the
    config happened to hash to this".
    """

    return {
        "version": "default",
        **DEFAULTS,
        "assets": {key: None for key in ASSET_KEYS},
    }


async def _load() -> Tuple[Dict[str, str], Dict[str, Dict], Dict[str, Any], Optional[str]]:
    """Read config + asset metadata in one round trip.

    Returns the resolved config, the asset metadata by key, the RAW stored blob
    (the admin editor needs to know which fields exist as overrides, which the
    resolved config cannot tell it) and the config row's ``updated_at``.
    """

    rows = await q(_BRAND_QUERY)
    row = rows[0] if rows else {}
    raw = _loads(row.get("data"))
    raw = raw if isinstance(raw, dict) else {}
    config = _coerce_stored(raw)
    assets_list = _loads(row.get("assets")) or []
    assets = {
        a["key"]: a
        for a in assets_list
        if isinstance(a, dict) and a.get("key") in ASSET_KEYS
    }
    return config, assets, raw, row.get("config_updated_at")


async def public_document() -> Dict[str, Any]:
    """The body of ``GET /brand``. **Never raises.**

    The login screen renders before any token exists and it renders from this,
    so a 500 here is a blank sign-in page for every user — a Postgres blip
    would take down authentication for a deployment whose database is otherwise
    fine. Any failure degrades to the shipped defaults, which is precisely how
    the product looked before this feature existed.
    """

    try:
        config, assets, _raw, _stamp = await _load()
    except Exception:  # noqa: BLE001 — see the docstring; defaults, never a 500
        return default_document()
    return {
        "version": _version(config, assets),
        **config,
        "assets": _asset_urls(assets),
    }


async def admin_document() -> Dict[str, Any]:
    """``GET /admin/branding``: the public document plus what the editor needs.

    Adds per-asset metadata (so the page can show "512×512 PNG, 18 KB" next to
    the preview) and, per field, whether the value is a stored override or the
    shipped default — the page needs to distinguish "the operator typed City
    Pharma" from "nobody has set this", because only the first should show a
    revert control.

    Unlike :func:`public_document` this one is allowed to fail: it is behind an
    admin token, and an editor that silently shows defaults while the database
    is unreachable would invite an operator to "fix" a config that is fine.
    """

    config, assets, stored, updated_at = await _load()

    # "Overridden" means a VALID stored value, not merely a key in the blob: a
    # field whose stored value failed `_coerce_stored` is rendering as the
    # default, and telling the editor otherwise would show a revert control for
    # a value nobody can see.
    overridden = [f for f in TEXT_FIELDS if f in stored and _stored_ok(f, stored[f])]
    return {
        "version": _version(config, assets),
        **config,
        "assets": _asset_urls(assets),
        "asset_meta": {
            key: (
                {
                    "mime": assets[key]["mime"],
                    "width": assets[key]["width"],
                    "height": assets[key]["height"],
                    "size_bytes": assets[key]["size_bytes"],
                    "sha256": assets[key]["sha256"],
                    "updated_at": assets[key]["updated_at"],
                }
                if key in assets
                else None
            )
            for key in ASSET_KEYS
        },
        "defaults": dict(DEFAULTS),
        "overridden": overridden,
        "is_default": {f: f not in overridden for f in TEXT_FIELDS},
        "config_updated_at": updated_at,
        "limits": {
            "max_bytes": MAX_ASSET_BYTES,
            "max_pixels": MAX_ASSET_PIXELS,
            "accepted": sorted(ALLOWED_MIME),
            "max_len": {f: MAX_LEN.get(f, DEFAULT_MAX_LEN) for f in TEXT_FIELDS},
        },
    }


async def get_asset(key: str) -> Optional[Dict[str, Any]]:
    """The stored bytes + mime for one asset, or ``None`` when unset."""

    if key not in ASSET_KEYS:
        return None
    rows = await q(
        "SELECT key, mime, bytes, sha256 FROM brand_assets WHERE key = $1", key
    )
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def apply_updates(updates: Dict[str, str]) -> None:
    """Merge validated text fields into the single config row.

    The merge happens in Postgres (``||``) rather than read-modify-write in
    Python so two operators saving different fields at the same moment cannot
    drop each other's edit.
    """

    await execute(
        """
        INSERT INTO brand_config (id, data, updated_at)
             VALUES (1, $1::jsonb, now())
        ON CONFLICT (id) DO UPDATE
                SET data = brand_config.data || EXCLUDED.data,
                    updated_at = now()
        """,
        json.dumps(updates, ensure_ascii=False),
    )


async def store_asset(key: str, image: "ValidatedImage") -> None:
    """Upsert one asset. ``bytes`` binds as BYTEA; never interpolated."""

    if key not in ASSET_KEYS:
        raise BrandError(f"unknown asset {key!r}")
    await execute(
        """
        INSERT INTO brand_assets
               (key, mime, bytes, sha256, width, height, size_bytes, updated_at)
             VALUES ($1, $2, $3, $4, $5, $6, $7, now())
        ON CONFLICT (key) DO UPDATE
                SET mime = EXCLUDED.mime,
                    bytes = EXCLUDED.bytes,
                    sha256 = EXCLUDED.sha256,
                    width = EXCLUDED.width,
                    height = EXCLUDED.height,
                    size_bytes = EXCLUDED.size_bytes,
                    updated_at = now()
        """,
        key,
        image.mime,
        image.data,
        image.sha256,
        image.width,
        image.height,
        len(image.data),
    )


async def delete_asset(key: str) -> bool:
    """Remove one asset. Returns whether a row was actually there."""

    if key not in ASSET_KEYS:
        raise BrandError(f"unknown asset {key!r}")
    status = await execute("DELETE FROM brand_assets WHERE key = $1", key)
    return status.rsplit(" ", 1)[-1] != "0"


async def reset_all() -> None:
    """Back to the shipped look: no overrides, no assets."""

    await execute("DELETE FROM brand_assets")
    await execute("DELETE FROM brand_config")


# ---------------------------------------------------------------------------
# Upload validation
# ---------------------------------------------------------------------------
#
# ⚠️ The type is decided by MAGIC BYTES. Not the filename, not the browser's
# `Content-Type`, both of which the uploader chooses. `logo.png` with an
# `image/png` header and `<svg onload=...>` inside is the whole attack, and it
# is trivial to send by hand; a filename check would pass it, store it, and then
# `GET /brand/asset/icon` would serve executable XML same-origin with the admin
# console. There is no filename or header check anywhere below, deliberately.
#
# ⚠️ Dimensions are read from the format HEADER, before anything decodes the
# pixels. A 1 KB PNG can declare 60,000 × 60,000; decoding it to find out how
# big it is allocates ~10 GB. The IHDR/SOF fields are 8–20 bytes in and are what
# a decoder would trust anyway.


class ValidatedImage:
    """An upload that passed every check, with the facts the DB row needs."""

    __slots__ = ("data", "mime", "width", "height", "sha256")

    def __init__(self, data: bytes, mime: str, width: int, height: int) -> None:
        self.data = data
        self.mime = mime
        self.width = width
        self.height = height
        self.sha256 = hashlib.sha256(data).hexdigest()


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def _png_dimensions(data: bytes) -> Tuple[int, int]:
    """Width/height from IHDR, which the PNG spec pins as the FIRST chunk."""

    if len(data) < 24:
        raise BrandError("truncated PNG header")
    if data[12:16] != b"IHDR":
        raise BrandError("PNG is missing its IHDR header chunk")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def _png_ends_cleanly(data: bytes) -> bool:
    """Walk the chunk table; the file must end exactly where IEND does.

    Without this a valid PNG with a payload appended after IEND stores fine and
    stays a valid PNG — every decoder ignores the tail — while the bytes we hand
    back to a browser are attacker-chosen. Since Pillow is not a dependency here
    (see the module note in ``app/admin.py``) nothing re-encodes the file, so
    this structural walk is the only thing standing between an appended blob and
    the response body.
    """

    pos = 8
    total = len(data)
    while pos + 8 <= total:
        length = int.from_bytes(data[pos:pos + 4], "big")
        ctype = data[pos + 4:pos + 8]
        # A length field big enough to overflow past the file is malformed, and
        # is also how you would make this loop wrap around.
        if length > total:
            return False
        pos += 12 + length          # length + type + data + crc
        if ctype == b"IEND":
            return pos == total
    return False


# SOF0..SOF15 carry the frame dimensions. C4 (DHT), C8 (JPG) and CC (DAC) sit
# inside that numeric range and are NOT frame headers — reading dimensions out
# of a Huffman table is how a "0x0 image" gets through.
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
             0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_dimensions(data: bytes) -> Tuple[int, int]:
    """Width/height from the first SOF marker, walking the segment table."""

    pos, total = 2, len(data)
    while pos + 1 < total:
        if data[pos] != 0xFF:
            raise BrandError("malformed JPEG segment table")
        marker = data[pos + 1]
        if marker == 0xFF:           # fill byte
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2                 # standalone marker, no length
            continue
        if marker == 0xD9 or marker == 0xDA:
            break                    # EOI, or entropy data begins — no SOF found
        if pos + 4 > total:
            break
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        if length < 2:
            raise BrandError("malformed JPEG segment length")
        if marker in _JPEG_SOF:
            if pos + 9 > total:
                raise BrandError("truncated JPEG frame header")
            height = int.from_bytes(data[pos + 5:pos + 7], "big")
            width = int.from_bytes(data[pos + 7:pos + 9], "big")
            return width, height
        pos += 2 + length
    raise BrandError("JPEG has no frame header")


def _jpeg_ends_cleanly(data: bytes) -> bool:
    """The file must end at its EOI marker.

    0xFFD9 cannot occur inside entropy-coded data (0xFF is byte-stuffed to
    0xFF00 there) and any thumbnail carrying one is inside a length-prefixed
    APPn segment we never scan into, so "first EOI is the last two bytes" is a
    sound trailing-byte check.
    """

    return data.endswith(b"\xff\xd9") and data.find(b"\xff\xd9", 2) == len(data) - 2


def validate_image(data: bytes) -> ValidatedImage:
    """Decide the type from magic bytes and prove the file is what it claims.

    Order matters: magic bytes → header dimensions → structural end-of-file.
    Nothing decodes pixels, so a decompression bomb is refused at ~20 bytes of
    work.
    """

    if len(data) > MAX_ASSET_BYTES:
        raise BrandTooLarge(
            f"file is larger than {MAX_ASSET_BYTES // 1024} KB"
        )
    if not data:
        raise BrandError("empty upload")

    if data.startswith(_PNG_MAGIC):
        mime = "image/png"
        width, height = _png_dimensions(data)
        ends_cleanly = _png_ends_cleanly(data)
    elif data.startswith(_JPEG_MAGIC):
        mime = "image/jpeg"
        width, height = _jpeg_dimensions(data)
        ends_cleanly = _jpeg_ends_cleanly(data)
    else:
        # The message names SVG explicitly because it is the format an operator
        # will actually try — every brand kit ships one — and "unsupported" with
        # no reason reads as a bug rather than a decision.
        raise BrandError(
            "only PNG and JPEG images are accepted (SVG is refused: it is "
            "scriptable markup, and these files are served from the same "
            "origin as the admin console)"
        )

    if width <= 0 or height <= 0:
        raise BrandError("image reports a zero dimension")
    if width > MAX_ASSET_PIXELS or height > MAX_ASSET_PIXELS:
        raise BrandError(
            f"image is {width}×{height}; the limit is "
            f"{MAX_ASSET_PIXELS}×{MAX_ASSET_PIXELS}"
        )
    if not ends_cleanly:
        raise BrandError("file carries data past the end of the image")

    return ValidatedImage(data, mime, width, height)


async def read_capped(upload, limit: int = MAX_ASSET_BYTES) -> bytes:
    """Read an ``UploadFile`` in chunks, aborting one byte past *limit*.

    ``await file.read()`` with no argument materialises whatever was sent. The
    cap has to be enforced *while* reading, not after, or a 2 GB body is already
    resident before anything gets to reject it.
    """

    chunks: List[bytes] = []
    seen = 0
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        seen += len(chunk)
        if seen > limit:
            raise BrandTooLarge(f"file is larger than {limit // 1024} KB")
        chunks.append(chunk)
    return b"".join(chunks)


def extension_for(mime: str) -> str:
    """Filename suffix for the download disposition."""

    return ALLOWED_MIME.get(mime, "bin")


__all__ = [
    "ASSET_KEYS",
    "DARK_LOGO_MODES",
    "DEFAULTS",
    "MAX_ASSET_BYTES",
    "MAX_ASSET_PIXELS",
    "TEXT_FIELDS",
    "BrandError",
    "BrandTooLarge",
    "ValidatedImage",
    "admin_document",
    "apply_updates",
    "default_document",
    "delete_asset",
    "ensure_brand_tables",
    "extension_for",
    "get_asset",
    "public_document",
    "read_capped",
    "reset_all",
    "store_asset",
    "validate_image",
    "validate_updates",
]
