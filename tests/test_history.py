"""Guard the synthetic-turn recorder that keeps the fast path conversational.

`record_turn` reaches into Agno's private `_session` / `_storage` modules. These
tests exist because two of its invariants fail SILENTLY — the turn is written to
Postgres and then ignored on read, with no error anywhere:

* a run whose ``agent_id`` is None loses the key on serialize, and
  ``AgentSession.from_dict`` only revives a run when ``"agent_id"`` is present;
* a run whose ``status`` is not ``completed`` (or whose ``parent_run_id`` is set)
  is skipped by ``get_messages``.

Needs live Postgres, like the rest of the suite.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.agent import HISTORY_AGENT_ID, build_history_agent
from app.history import record_turn

Q = "where do we have RELYTE ORAL REHYDRATION SALTS 20.5G"
A = "RELYTE (1000000369323) is at 20026-CC19: 6533 units."


def run(coro):
    return asyncio.run(coro)


async def _replay(agent, session_id):
    """Read the session back from the DB the way add_history_to_context does."""

    from agno.agent import _session as agno_session

    session = await agno_session.aget_session(agent, session_id=session_id, user_id="u1")
    return [] if session is None else session.get_messages(last_n_runs=3)


def test_recorded_turn_survives_a_database_round_trip():
    async def go():
        agent = build_history_agent()
        if getattr(agent, "db", None) is None:
            pytest.skip("no Agno DB available")
        sid = "t-" + uuid.uuid4().hex[:10]
        stored = await record_turn(agent, sid, "u1", Q, A)
        return stored, await _replay(agent, sid)

    stored, msgs = run(go())
    assert stored is True
    # Both messages come back, in order, or history replay sees nothing.
    assert [(m.role, m.content) for m in msgs] == [("user", Q), ("assistant", A)]


def test_history_stores_the_question_not_the_phrasing_prompt():
    """The fast path's FACTS blob must never reach the next turn's context."""

    async def go():
        agent = build_history_agent()
        if getattr(agent, "db", None) is None:
            pytest.skip("no Agno DB available")
        sid = "t-" + uuid.uuid4().hex[:10]
        await record_turn(agent, sid, "u1", Q, A)
        return await _replay(agent, sid)

    blob = " ".join((m.content or "") for m in run(go()))
    assert "FACTS" not in blob
    assert "answer from these only" not in blob


def test_history_agent_has_a_stable_id():
    """A None agent_id serialises away, and the run is then dropped on read."""

    agent = build_history_agent()
    if getattr(agent, "db", None) is None:
        pytest.skip("no Agno DB available")
    assert agent.id == HISTORY_AGENT_ID


def test_record_turn_is_a_noop_without_a_session():
    async def go():
        agent = build_history_agent()
        return (
            await record_turn(agent, None, "u1", Q, A),
            await record_turn(agent, "s", "u1", "", A),
            await record_turn(agent, "s", "u1", Q, ""),
        )

    assert run(go()) == (False, False, False)


def test_record_turn_never_raises_on_a_broken_agent():
    """A forgotten turn is a degraded chat; an exception is a failed answer."""

    class Broken:
        db = object()      # truthy, so we get past the early return

        @property
        def id(self):
            raise RuntimeError("boom")

    assert run(record_turn(Broken(), "s", "u1", Q, A)) is False
