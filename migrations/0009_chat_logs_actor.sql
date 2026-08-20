-- 0009 — who asked, and did the answer give up.
--
-- ⚠️ Applied at boot by `app.activity.ensure_chat_logs_actor()`. This file alone
-- changes nothing on a running stack; keep the two in step.
--
-- `actor_email` / `actor_role`
-- ----------------------------
-- Console-originated turns only. The admin chat tester posts through the same
-- embed API as the widget (it is a first-party embed client, `embed_id =
-- 'admin-chat'`) and carries the operator's own bearer token, so "who was
-- driving the tester when this answer came out wrong" is knowable and is
-- currently thrown away.
--
-- ⚠️ EMBED AND WIDGET TURNS STAY NULL. That is not a gap to be filled later.
-- The widget is a PUBLIC pharmacy surface: the people typing into it are members
-- of the public asking what a medicine is for. NULL is the correct, complete
-- record for them, and a `visitor_id` cookie is deliberately NOT part of this
-- migration — identifying anonymous visitors is a disclosure decision for the
-- product owner, not an engineering convenience.
--
-- `gave_up`
-- ---------
-- Three states, and the third is the one that matters:
--
--   true   — the answer the USER SAW was an apology. The turn may have recorded
--            success, called its tools and returned in 4 seconds; what reached
--            the screen was still "I could not produce a reliable answer".
--            Every leak-filter fallback and every empty answer is one of these.
--   false  — evaluated, and the user got a real answer.
--   NULL   — NOT EVALUATED. The 122 pre-instrumentation turns are NULL and stay
--            NULL; they are unknown, and unknown is a value the UI can render.
--
-- Defaulting this to `false` would silently assert that every turn ever logged
-- was fine, including the ones nobody ever looked at.
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS actor_email text;
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS actor_role  text;
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS gave_up     boolean;
CREATE INDEX IF NOT EXISTS chat_logs_actor_idx ON chat_logs (actor_email, ts DESC);
