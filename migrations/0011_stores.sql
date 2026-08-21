-- 0011 — `stores`: the branch registry. Existence, separated from stock.
--
-- ⚠️ Applied at boot by `app.stores.ensure_stores_table()`. This file alone
-- changes nothing on a running stack; keep the two in step.
--
-- Why the table exists
-- --------------------
-- A branch used to EXIST only because it had rows in `inventory`, and
-- `inventory` is truncate-and-reload from the daily balance export
-- (`ingest.ingest_inventory` TRUNCATEs then COPYs). So a branch dropped from one
-- file — a till that did not sync, a partial export — silently disappeared from
-- the console, the outlet picker and every chat answer, and came back the next
-- morning. There was also no way for an admin to hide a branch.
--
-- After this: existence comes from here and ACCUMULATES; stock still comes from
-- `inventory` and is still replaced daily.
--
-- Two rules are encoded in the columns, and both are product decisions
-- --------------------------------------------------------------------
-- 1. `status='disabled'` means PRETEND THIS BRANCH DOES NOT EXIST — absent from
--    chat answers, from lists, and from company-wide totals. Only two values are
--    legal and the CHECK enforces it, so a typo cannot become a third status
--    that no filter in the codebase matches.
-- 2. `missing_since` FLAGS a branch that fell out of the latest file. It does
--    NOT hide it. Nothing but an admin's own action ever writes `status` — a
--    broken export must never have the power to take a live pharmacy off the
--    map, which is the failure this whole table exists to prevent.
--
-- ⚠️⚠️ THE SEED IS NOT A CONVENIENCE. AN EMPTY REGISTRY IS THE DANGEROUS STATE.
--
-- Once the product filters visibility through this table, the obvious filter —
-- `JOIN stores s ON s.site_code = i.site_code AND s.status='active'` — returns
-- NOTHING when `stores` is empty. A registry that failed to populate would make
-- the whole product answer "we have no stock anywhere", silently, with every
-- query still succeeding. That is worse than the bug being fixed.
--
-- So this migration SEEDS every site_code already present in `inventory` as
-- active, in the same transaction that creates the table. A database with stock
-- gets a populated registry the moment this runs, and switching visibility over
-- cannot make a single existing branch vanish. `app.stores` additionally reads
-- the registry as "everything, minus what is explicitly disabled", so even an
-- empty table hides nothing.
--
-- ⚠️⚠️ THE SEED WRITES `first_seen = NULL`. IT IS NOT AN OVERSIGHT.
--
-- A branch that predates the registry has an UNKNOWN first_seen, not today's,
-- and `first_seen` is NULLABLE (keeping its DEFAULT) to say so. Stamping now()
-- on a seeded row is the registry asserting a fact it does not have, and the
-- assertion is load-bearing downstream: the console derives "New" from a
-- first_seen inside the last 7 days. With now(), a fresh install shows all 53
-- long-standing branches as brand new for a week and the Active filter returns
-- an EMPTY table. That is what the first cut of this migration did, on dev, with
-- all 53 rows sharing one timestamp; it was found by looking at the rendered
-- page, not by reading this SQL.
--
-- `app.stores.sync_from_file` still takes the DEFAULT and stamps a real now()
-- for a code the registry has genuinely never seen. "We watched this branch
-- appear" and "this branch was here before we started counting" are different
-- facts and this column is the only place they are distinguishable — do not
-- collapse them.
--
-- `last_seen_in_file` is left NULL by the seed for the same reason: those rows
-- are inferred from stock that is already loaded and we do not know which file
-- carried it. A seeded branch is unobserved, not missing, so `missing_since`
-- stays NULL too.
--
-- NO BACKFILL of first_seen for rows this migration did not insert. Once a row
-- exists there is no way to tell a seeded timestamp from a genuinely observed
-- one, and guessing would destroy the real dates along with the wrong ones.
--
-- Safe to run twice: CREATE TABLE IF NOT EXISTS, and the seed inserts only codes
-- the table does not already hold, so a branch an admin disabled is never
-- quietly re-enabled by re-running this.
--
-- No index beyond the primary key on purpose. One row per branch — 53 today —
-- so every read is a sequential scan of well under a page, and a second btree
-- would be write cost for no read benefit.

CREATE TABLE IF NOT EXISTS stores (
    site_code          TEXT PRIMARY KEY,
    site_name          TEXT,
    status             TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','disabled')),
    first_seen         TIMESTAMPTZ DEFAULT now(),   -- NULL = predates the registry
    last_seen_in_file  TIMESTAMPTZ,
    missing_since      TIMESTAMPTZ,
    disabled_by        TEXT,
    disabled_at        TIMESTAMPTZ,
    note               TEXT
);

-- Converge a database that ran the first cut of this file, where `first_seen`
-- was NOT NULL. Idempotent, and a no-op on a table just created above.
ALTER TABLE stores ALTER COLUMN first_seen DROP NOT NULL;

COMMENT ON COLUMN stores.first_seen IS
    'When we first observed this branch. NULL = it predates the registry (seeded from existing stock) — never read a NULL as today.';
COMMENT ON TABLE stores IS
    'Branch registry: which branches exist (accumulates) and which are visible to customers.';
COMMENT ON COLUMN stores.missing_since IS
    'NULL when the branch was in the most recent inventory file. A flag only — never hides the branch.';
COMMENT ON COLUMN stores.status IS
    'active | disabled. disabled = invisible to customers, including in company-wide totals.';

INSERT INTO stores (site_code, site_name, first_seen)
SELECT i.site_code, MAX(i.site_name), NULL   -- NULL first_seen: see the warning above
  FROM inventory i
 WHERE NOT EXISTS (SELECT 1 FROM stores s WHERE s.site_code = i.site_code)
 GROUP BY i.site_code
ON CONFLICT (site_code) DO NOTHING;
