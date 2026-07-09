-- ============================================================================
-- 0002_drug_alias.sql — learned drug-name aliases for the deterministic fast path
-- ============================================================================
-- Long-term memory that makes resolution faster with use: when a pharmacist
-- clarifies which article a free-text mention meant, that mention is written
-- here so the next identical question resolves in ~5ms with no LLM and no
-- trigram scan. One alias -> one article_code.
--
-- Idempotent (IF NOT EXISTS): safe to re-run against a live database.
-- ============================================================================

CREATE TABLE IF NOT EXISTS drug_alias (
    alias        TEXT PRIMARY KEY,                       -- normalised (lowercased) mention
    article_code TEXT NOT NULL
                 REFERENCES catalog(article_code) ON DELETE CASCADE,
    source       TEXT,                                   -- who taught it (e.g. 'pharmacist', 'seed')
    created_at   TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE drug_alias IS 'Learned free-text -> article_code aliases (fast-path memory).';

-- Lookup path: resolve by article_code (e.g. to list/prune a code's aliases).
CREATE INDEX IF NOT EXISTS idx_drug_alias_article ON drug_alias (article_code);
