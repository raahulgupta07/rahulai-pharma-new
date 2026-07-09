-- ============================================================================
-- 0001_inventory_nullable_stock_price.sql
-- ============================================================================
-- Defect A3: inventory.stock_qty / inventory.price were NOT NULL DEFAULT 0, so
-- a blank cell in the balance_stock export loaded as 0 — indistinguishable from
-- "zero on hand". Drop NOT NULL and DEFAULT so NULL can mean "unknown".
--
-- Safe to run more than once: ALTER ... DROP NOT NULL / DROP DEFAULT are no-ops
-- when the constraint/default is already gone. Existing rows are untouched.
--
-- Apply (no migration framework in this repo — run the file directly):
--   docker exec -i pharmacy-opt-postgres-1 \
--     psql -U pharmacy -d pharmacy < migrations/0001_inventory_nullable_stock_price.sql
-- ============================================================================

ALTER TABLE inventory ALTER COLUMN stock_qty DROP NOT NULL;
ALTER TABLE inventory ALTER COLUMN stock_qty DROP DEFAULT;
ALTER TABLE inventory ALTER COLUMN price     DROP NOT NULL;
ALTER TABLE inventory ALTER COLUMN price     DROP DEFAULT;
