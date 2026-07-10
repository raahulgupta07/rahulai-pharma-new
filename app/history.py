"""Write a turn into an Agno session without running the model.

The fast path answers with its own tool-less phrasing agent, and a cache hit
answers with no agent at all. Neither writes anything to the conversation, so a
follow-up ("which other shop has it?") would load an empty history and have no
idea what "it" is.

Rather than give up the fast path for conversations — it is the whole latency
win — we record the turn ourselves: the user's ACTUAL question and the answer we
served, as one completed run.

Why not just hand the phrasing agent a ``db`` and a ``session_id`` and let Agno
persist it? Because Agno stores what it was given, and what the phrasing agent is
given is ``build_phrasing_input()``: a language directive plus a FACTS JSON blob
carrying every row the SQL returned. Replaying that as history would show the
model a "user message" the user never sent, leak prompt scaffolding into context,
and drag a 53-row JSON payload into the next three prompts — on a stack where
per-call cost already dominates latency.

⚠️ This module reaches into Agno's private ``_session`` / ``_storage`` modules.
They carry no API-stability guarantee. Everything is funnelled through
:func:`record_turn` so an upgrade breaks in exactly one place, and every failure
degrades to "the chat forgot this turn" rather than an error to the user.

Invariants this relies on, verified against the installed agno:

* ``asave_session`` silently does nothing when ``session.session_data is None``,
  so the session must come from ``aread_or_create_session`` (which sets ``{}``),
  never from a hand-built ``AgentSession``.
* ``AgentSession.get_messages`` drops runs whose ``status`` is in
  ``(paused, cancelled, error)`` and runs with a non-None ``parent_run_id``.
  A run that violates either is stored happily and never replayed — silent.
* ``upsert_run`` replaces an existing run with the same ``run_id``, so the id
  must be fresh or we overwrite a real turn.
* Messages must NOT be tagged ``from_history=True``; that flag marks messages a
  previous run pulled IN as context, and tagged messages are skipped on read.
"""

from __future__ import annotations

import logging
from typing import Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


async def record_turn(
    agent,
    session_id: Optional[str],
    user_id: Optional[str],
    question: str,
    answer: str,
) -> bool:
    """Append one (user, assistant) exchange to ``session_id``'s history.

    ``agent`` must be the history-aware agent — the one holding the ``db`` that
    later runs read their history from. Returns True when the turn was stored.

    Never raises. A conversation that forgets a turn is a degraded chat; an
    exception here would be a failed answer, which is worse.
    """

    if not session_id or not question or not answer:
        return False

    try:
        db = getattr(agent, "db", None)
        if db is None:
            # The stateless agent. Nothing will ever read this history back.
            return False

        from agno.agent import _session as agno_session
        from agno.agent import _storage as agno_storage
        from agno.models.message import Message
        from agno.run.agent import RunOutput
        from agno.run.base import RunStatus

        # get-or-create: aget_session returns None for a session that has never
        # had a real run, and asave_session would then no-op on a hand-built one.
        session = await agno_storage.aread_or_create_session(
            agent, session_id, user_id=user_id
        )
        if session is None:
            logger.warning("Could not open session %s; turn not recorded", session_id)
            return False

        from app.agent import HISTORY_AGENT_ID

        run = RunOutput(
            run_id=str(uuid4()),      # fresh, or upsert_run overwrites a real run
            session_id=session_id,
            # MUST be non-None. RunOutput.to_dict drops None fields, and
            # AgentSession.from_dict only revives a run when "agent_id" is a key
            # — so a None here stores the turn and then loses it on read, with no
            # error anywhere.
            agent_id=getattr(agent, "id", None) or HISTORY_AGENT_ID,
            user_id=user_id,
            status=RunStatus.completed,   # anything else is skipped on read
            parent_run_id=None,           # non-None is skipped on read
            content=answer,
            messages=[
                # The user's real question, not the phrasing prompt.
                Message(role="user", content=question),
                Message(role="assistant", content=answer),
            ],
        )

        session.upsert_run(run=run)
        await agno_session.asave_session(agent, session=session)
        return True
    except Exception:   # noqa: BLE001 — a forgotten turn beats a failed answer
        logger.exception("Failed to record turn for session %s", session_id)
        return False


__all__ = ["record_turn"]
