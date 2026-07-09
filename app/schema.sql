-- ============================================================================
-- schema.sql — Postgres schema for CityCare pharmacy agent (real-data shape)
-- ============================================================================
-- Idempotent: drops tables then recreates so a full reload is always clean.
--   catalog   — one row per article (SKU master), incl. clinical fields
--   inventory — stock + price per article per site (balance_stock export)
-- NOTE: inventory has NO hard FK to catalog. The real balance_stock export
-- references thousands of article_codes that are absent from the article
-- export, so a strict FK would reject valid stock rows. Joins still work; an
-- index keeps them fast.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector: semantic search on catalog

DROP TABLE IF EXISTS inventory CASCADE;
DROP TABLE IF EXISTS catalog CASCADE;

-- ----------------------------------------------------------------------------
-- catalog: article (SKU) master data. Mirrors the "Article Export" columns.
-- ----------------------------------------------------------------------------
CREATE TABLE catalog (
    article_code TEXT PRIMARY KEY,   -- unique SKU / barcode
    brand_name   TEXT NOT NULL,      -- commercial brand label
    generic_name TEXT,               -- active ingredient family
    composition  TEXT,               -- strength / formulation
    category     TEXT,               -- e.g. 1102-OIL, 1104-SUGAR
    indication   TEXT,               -- what it treats (often Burmese)
    dosage       TEXT,               -- how to take (often Burmese)
    side_effect  TEXT,               -- adverse effects (often Burmese)
    mm_reg       TEXT,               -- Myanmar registration ref
    mm_label     TEXT,               -- MM label flag / text
    status       TEXT,                -- active flag from source
    embedding    vector(3072)         -- gemini-embedding-2 of name+generic+indication
);

COMMENT ON TABLE catalog IS 'Article (SKU) master: one row per product.';

-- ----------------------------------------------------------------------------
-- inventory: stock quantity and price for an article at a specific site.
-- Sourced from balance_stock export (price = weighted_cost_price).
-- ----------------------------------------------------------------------------
-- Daily full-replace snapshot. Natural composite PK (article_code, site_code)
-- = dedup + the article_code index, with no unused surrogate id.
CREATE TABLE inventory (
    article_code TEXT NOT NULL,           -- joins catalog.article_code
    site_code    TEXT NOT NULL,           -- branch / outlet code
    site_name    TEXT,                    -- optional human-readable branch name
    stock_qty    INTEGER,               -- NULL = unknown (blank in export), not 0
    price        NUMERIC(14,2),         -- weighted cost price; NULL = unknown
    uom          TEXT DEFAULT 'MMK',
    PRIMARY KEY (article_code, site_code)
);

COMMENT ON TABLE inventory IS 'Per-site stock and price for each article.';
COMMENT ON COLUMN inventory.price IS 'weighted_cost_price from balance_stock export.';

-- ----------------------------------------------------------------------------
-- Indexes for common filter / lookup paths.
-- ----------------------------------------------------------------------------
CREATE INDEX idx_inventory_article ON inventory (article_code);
CREATE INDEX idx_inventory_site    ON inventory (site_code);
CREATE INDEX idx_inventory_price   ON inventory (price);
CREATE INDEX idx_inventory_stock   ON inventory (stock_qty);
CREATE INDEX idx_catalog_generic   ON catalog (generic_name);

CREATE INDEX idx_catalog_brand_trgm   ON catalog USING gin (brand_name gin_trgm_ops);
CREATE INDEX idx_catalog_generic_trgm ON catalog USING gin (generic_name gin_trgm_ops);

-- ----------------------------------------------------------------------------
-- Materialized views — precomputed aggregates, refreshed after each data load.
-- Created empty here; populated by REFRESH after inventory is loaded.
-- ----------------------------------------------------------------------------
DROP MATERIALIZED VIEW IF EXISTS mv_store_summary;
CREATE MATERIALIZED VIEW mv_store_summary AS
SELECT i.site_code,
       COUNT(*)                              AS skus,
       SUM(i.stock_qty)                       AS units,
       ROUND(SUM(i.price * i.stock_qty))      AS value
  FROM inventory i
 GROUP BY i.site_code
 WITH NO DATA;
CREATE UNIQUE INDEX idx_mv_store ON mv_store_summary (site_code);

DROP MATERIALIZED VIEW IF EXISTS mv_article_summary;
CREATE MATERIALIZED VIEW mv_article_summary AS
SELECT i.article_code,
       COALESCE(c.brand_name, i.article_code) AS brand_name,
       c.generic_name,
       SUM(i.stock_qty)                       AS total_stock,
       ROUND(SUM(i.price * i.stock_qty)
             / NULLIF(SUM(i.stock_qty), 0), 2) AS weighted_avg_price,
       COUNT(DISTINCT i.site_code)            AS site_count
  FROM inventory i
  LEFT JOIN catalog c USING (article_code)
 GROUP BY i.article_code, c.brand_name, c.generic_name
 WITH NO DATA;
CREATE UNIQUE INDEX idx_mv_article ON mv_article_summary (article_code);

-- No ANN index on embedding: pgvector hnsw/ivfflat cap at 2000 dims and
-- gemini-embedding-2 is 3072. With ~4.9k catalog rows an exact KNN scan
-- (ORDER BY embedding <=> q) is sub-10ms. If the catalog grows large, switch
-- the column to halfvec(3072) and add a hnsw index (halfvec supports up to 4000).
