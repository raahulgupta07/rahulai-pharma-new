-- ============================================================================
-- 0005_branding.sql
-- ============================================================================
-- White-label branding: the product name, the sentences the login screen and
-- the console chrome show, and four logo assets — all operator-editable from
-- Configuration → Branding.
--
-- **The bytes live in Postgres, not on disk, and that is the point.**
-- `docker-compose.yml` mounts no source volume for `api`; both `app/` and the
-- built admin SPA are COPYed into the image (CLAUDE.md, "Deploy gotcha"). A
-- logo written to a container path is destroyed by the next
-- `docker compose build`, which is every deploy. The operator uploads a logo,
-- watches it work, and loses it at the next release with nothing in the logs
-- to explain it. A BYTEA column survives a rebuild; a bind mount would survive
-- too, but only on the host that happens to have it.
--
--   brand_config  a SINGLE row (`CHECK (id = 1)`), holding ONLY the fields an
--                 operator has actually overridden. Everything absent falls
--                 back in code (`app/brand.py::DEFAULTS`) to the exact string
--                 the UI hardcoded before this table existed, so an
--                 unconfigured install renders identically to the old build.
--
--   brand_assets  one row per logo slot: `icon`, `lockup`, `lockup_dark`,
--                 `parent`.
--                 mime        the STORED content type, decided from magic
--                             bytes on upload — never from the filename and
--                             never from the browser's Content-Type. PNG and
--                             JPEG only: an SVG is scriptable XML served
--                             same-origin with the admin console, i.e. stored
--                             XSS against a super_admin whose bearer token is
--                             in localStorage.
--                             ⚠️ Do not widen this to image/svg+xml.
--                 sha256      of the stored bytes. It is the `?v=` in the
--                             asset URL, which is what makes
--                             `Cache-Control: immutable` safe — a replaced
--                             logo is a different URL, not a stale cache.
--                 width/height parsed from the PNG IHDR / JPEG SOF header
--                             BEFORE any decode, and capped at 1024px, so a
--                             1 KB file declaring 60000×60000 is refused
--                             without ever being expanded.
--
-- Safe to run more than once: CREATE TABLE IF NOT EXISTS only.
--
-- `app.brand.ensure_brand_tables()` performs the SAME statements on every boot,
-- for the same reason `ensure_auth_events` does: a fresh container and a
-- database someone already migrated by hand have to converge on one schema, and
-- a first boot must not need a human to run psql before the login screen works.
--
-- Apply (no migration framework in this repo — run the file directly):
--   docker exec -i pharmacy-opt-postgres-1 \
--     psql -U pharmacy -d pharmacy < migrations/0005_branding.sql
-- ============================================================================

CREATE TABLE IF NOT EXISTS brand_config (
    id         INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    data       JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS brand_assets (
    key        TEXT PRIMARY KEY,
    mime       TEXT NOT NULL,
    bytes      BYTEA NOT NULL,
    sha256     TEXT NOT NULL,
    width      INT,
    height     INT,
    size_bytes INT,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- No indexes: `brand_config` holds one row and `brand_assets` at most four,
-- both read by primary key. An index here would only be write overhead.
