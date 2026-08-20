-- 0010 — chat_feedback.turn_id: the real foreign key from a rating to its turn.
--
-- ⚠️ Applied at boot by `app.activity.ensure_chat_feedback_turn()`. This file
-- alone changes nothing on a running stack; keep the two in step.
--
-- Why the column exists
-- --------------------
-- `chat_feedback` carries its OWN copies of question and answer, so the only way
-- to connect a rating to a turn was to match that text. That guesses — two turns
-- with the same words become one rating — and it is what made `?rated=down`
-- return the entire table, because the correlated EXISTS resolved both sides to
-- `chat_feedback` and filtered on nothing. Without a real key the feedback KPI
-- cannot honour lang/embed/path/actor/cached, so it sits under filter chips it
-- silently ignores.
--
-- ⚠️⚠️ THE DELETE RULE IS `SET NULL`, AND `CASCADE` HERE DESTROYS USER DATA.
--
-- `admin.prune_chat_logs()` runs on EVERY BOOT and deletes `chat_logs` older
-- than `chat_log_retention_days` (default 30). Under `ON DELETE CASCADE` that
-- delete propagates here, so every rating attached to a turn older than the log
-- retention is silently destroyed with it — including the written corrections,
-- which are the single most valuable rows in this database. A rating is a human
-- taking the trouble to tell us we got something wrong; it is not a log line,
-- and it must not inherit a log's retention.
--
-- Demonstrated live, not reasoned about: with CASCADE, one INSERT of a
-- `verdict='down'` row with a correction, then the DELETE that `prune_chat_logs`
-- issues, left 0 feedback rows.
--
-- Under `SET NULL` the same prune leaves the rating intact and merely
-- unattributed — which is the truth about it once the turn is gone, and a state
-- the analytics layer already knows how to render.
--
-- No backfill
-- -----------
-- Existing rows keep NULL. Matching them on question/answer text is the exact
-- mistake this column exists to end, and a wrong attribution is worse than an
-- absent one: unknown is a value the UI can render, a confident wrong answer is
-- not. Populated going forward from the id `admin.log_chat` now returns.

ALTER TABLE chat_feedback
    ADD COLUMN IF NOT EXISTS turn_id bigint REFERENCES chat_logs(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS chat_feedback_turn_idx ON chat_feedback (turn_id);
