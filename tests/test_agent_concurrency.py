"""Concurrency safety of the SHARED Agno Agent under different session_ids.

BACKGROUND / THE OPEN QUESTION
------------------------------
``app/agent.py`` caches Agent objects with ``@lru_cache`` (``_agent_for`` /
``_learning_agent_for``), so ONE Agno ``Agent`` instance is shared across every
concurrent request. The learning agent is built with
``add_history_to_context=True, num_history_runs=3`` (agent.py:286-287), and
``app/api.py`` passes ``user_id`` / ``session_id`` PER ``arun()`` call
(api.py:583-588, via ``_learn_ids``).

QUESTION: with a single shared Agent, can one session's conversation history
leak into another session's context when two ``arun()`` calls with DIFFERENT
``session_id`` s run concurrently?

ANSWER (proven below, no live LLM): NO — it is safe, *because the app always
passes an explicit session_id*. Evidence from the installed agno 2.6.18 source:

  * ``agno/agent/_run.py:2768`` (``arun_dispatch``) →
    ``session_id, user_id = initialize_session(agent, session_id=session_id, ...)``
    forwards the caller's session_id.
  * ``agno/agent/_session.py:53-59`` (``initialize_session``) — the ONLY write to
    the shared ``agent.session_id`` is guarded by ``if session_id is None``. When
    a session_id is supplied it is returned unchanged and the shared instance is
    NOT mutated, so there is no sticky session_id shared between runs.
  * ``agno/agent/_run.py:1456`` (``_arun``) takes ``session_id`` and a per-call
    ``run_context`` as parameters; it loads the session fresh via
    ``aread_or_create_session(agent, session_id=session_id, ...)`` (line 1530)
    and builds history into a per-call ``run_messages`` local via
    ``aget_run_messages(..., session=agent_session, add_history_to_context=...)``
    (line 1596). History/session_state live on the per-run ``run_context`` and
    ``run_messages``, never on the shared ``Agent`` instance.

So conversation history is keyed by the per-call ``session_id`` and loaded from
the DB on every run; nothing about one run's history is stored on the shared
Agent object. The two tests below prove this end-to-end (no network) and pin the
mechanism so a future agno upgrade that regresses it fails LOUDLY.
"""

from __future__ import annotations

import asyncio

from agno.agent import Agent
from agno.db.in_memory import InMemoryDb
from agno.models.openrouter import OpenRouter
from agno.models.response import ModelResponse


class _StubModel(OpenRouter):
    """An OpenRouter model whose network call is replaced by a local stub.

    Overriding ``aresponse`` means NO live LLM / OpenRouter request is ever
    made. It records the exact message blob the model was handed for each run
    (identified by a per-run marker in the current question) so the test can
    assert what history was — or was NOT — placed in that run's context.
    """

    def __init__(self) -> None:
        super().__init__(id="stub/concurrency-test", api_key="stub-key-not-used")
        # marker -> full text of the messages that run sent to the model
        self.captured: dict[str, str] = {}

    async def aresponse(self, messages, **kwargs):  # type: ignore[override]
        blob = "\n".join(str(getattr(m, "content", "")) for m in messages)
        for marker in ("ALPHA_Q", "BETA_Q"):
            if marker in blob:
                self.captured[marker] = blob
        return ModelResponse(role="assistant", content="ok")


def _shared_learning_like_agent(model: _StubModel) -> Agent:
    """Build ONE agent wired like the app's cached learning agent.

    Mirrors ``build_learning_agent`` where it matters for this question: a DB is
    attached and history is added to context (``add_history_to_context=True,
    num_history_runs=3``). Uses an in-memory DB and no tools so the test needs
    no Postgres and no network.
    """

    return Agent(
        model=model,
        tools=[],
        db=InMemoryDb(),
        system_message="concurrency test agent",
        add_history_to_context=True,
        num_history_runs=3,
        markdown=True,
    )


async def _run_leak_scenario():
    model = _StubModel()
    agent = _shared_learning_like_agent(model)

    # Seed per-session history (sequential so the DB has both sessions' turns).
    await agent.arun("ALPHA_SECRET_PINEAPPLE remember this",
                     session_id="sessA", user_id="brain:20060-CCBHSC")
    await agent.arun("BETA_SECRET_WALRUS remember this",
                     session_id="sessB", user_id="brain:20063-CCBRBKMY")

    # Now recall from BOTH sessions concurrently on the SAME shared agent.
    await asyncio.gather(
        agent.arun("ALPHA_Q what did I say earlier",
                   session_id="sessA", user_id="brain:20060-CCBHSC"),
        agent.arun("BETA_Q what did I say earlier",
                   session_id="sessB", user_id="brain:20063-CCBRBKMY"),
    )

    return model.captured


def test_shared_agent_does_not_leak_history_across_sessions():
    """Two concurrent arun() calls on ONE shared Agent, different session_ids.

    Each session first records a distinct secret. Then both sessions recall,
    CONCURRENTLY, on the same shared Agent instance. Assert each run's model
    context contains only ITS OWN session's secret — never the other's.

    If agno ever regressed to sharing history off the Agent instance, one of
    these assertions fails loudly with the leaked secret named.
    """

    captured = asyncio.run(_run_leak_scenario())
    alpha_ctx = captured.get("ALPHA_Q", "")
    beta_ctx = captured.get("BETA_Q", "")

    assert alpha_ctx, "session A recall never reached the model"
    assert beta_ctx, "session B recall never reached the model"

    # Each session sees its own history...
    assert "ALPHA_SECRET_PINEAPPLE" in alpha_ctx, (
        "session A lost its own history — add_history_to_context not working"
    )
    assert "BETA_SECRET_WALRUS" in beta_ctx, (
        "session B lost its own history — add_history_to_context not working"
    )

    # ...and CRUCIALLY not the other session's history.
    assert "BETA_SECRET_WALRUS" not in alpha_ctx, (
        "HISTORY LEAK: session B's secret appeared in session A's model context. "
        "The shared Agno Agent is NOT safe under concurrent arun() with "
        "different session_ids."
    )
    assert "ALPHA_SECRET_PINEAPPLE" not in beta_ctx, (
        "HISTORY LEAK: session A's secret appeared in session B's model context. "
        "The shared Agno Agent is NOT safe under concurrent arun() with "
        "different session_ids."
    )


async def _run_mechanism_scenario():
    from agno.agent._session import initialize_session

    model = _StubModel()
    agent = _shared_learning_like_agent(model)
    assert agent.session_id is None

    # Supplying a session_id must return it unchanged and leave the shared
    # instance's session_id untouched.
    sid, _ = initialize_session(agent, session_id="explicit-session", user_id="brain:X")
    assert sid == "explicit-session"
    assert agent.session_id is None, (
        "agno made a caller-supplied session_id sticky on the shared Agent — "
        "this is a cross-session leak vector for the lru_cache'd agent"
    )

    # A real run with an explicit session_id must also leave it untouched.
    await agent.arun("hello", session_id="run-session", user_id="brain:Y")
    return agent.session_id


def test_passing_session_id_never_mutates_shared_agent_session_id():
    """Pin the exact mechanism that makes the shared Agent safe.

    ``agno/agent/_session.py:initialize_session`` only writes the shared
    ``agent.session_id`` when NO session_id is passed. The app ALWAYS passes one
    (``app/api.py`` ``_learn_ids`` never returns an empty session_id), so the
    shared instance never acquires a sticky session_id that could bleed between
    concurrent runs.

    This test would fail if a future agno version started making a
    caller-supplied session_id sticky on the shared instance.
    """

    final_session_id = asyncio.run(_run_mechanism_scenario())
    assert final_session_id is None, (
        "arun() with an explicit session_id mutated the shared Agent.session_id"
    )
