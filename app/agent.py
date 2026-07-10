"""Agno agent wiring: OpenRouter model + 12 tools + bilingual EN/Burmese system prompt.

Builds the configured Agno agent: an OpenRouter chat model (id + key from
:func:`app.config.get_settings`), the twelve pharmacy tools from
:mod:`app.tools`, and the :data:`BILINGUAL_SYSTEM_PROMPT`.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from agno.agent import Agent
from agno.models.openrouter import OpenRouter

logger = logging.getLogger(__name__)

from app.config import get_settings
from app.tools import (
    drugs_for_same_condition,
    filter_by_price,
    find_at_other_stores,
    get_article_info,
    get_stock,
    get_substitutes,
    list_sites,
    related_drugs,
    search_by_meaning,
    search_by_name,
    summarize_article,
    top_by_stock,
)

# Tools exposed to the model. Agno reads each function's signature and docstring
# to build the tool schema, so the docstrings in app.tools are load-bearing.
TOOLS = [
    get_article_info,
    search_by_name,
    get_stock,
    top_by_stock,
    filter_by_price,
    get_substitutes,
    summarize_article,
    search_by_meaning,
    related_drugs,
    drugs_for_same_condition,
    list_sites,
    find_at_other_stores,
]

BILINGUAL_SYSTEM_PROMPT = """\
You are the City Pharma Assistant, a bilingual (English / Burmese) helper \
for pharmacy staff and customers.

LANGUAGE
- Detect the user's language from their message: English or Burmese (မြန်မာ).
- Always reply in the SAME language the user wrote in.
- If the incoming prompt contains a bracketed directive like [Reply ONLY in Burmese] or [Reply ONLY in English], that directive is ABSOLUTE — obey it over any other consideration, including anything you may remember about the user. Never switch languages because of memory.

LITERAL VALUES
- Never translate or alter article codes, prices, units, or quantities. Keep \
them exactly as returned by the tools (e.g. an article code stays in its \
original form in any language).
- Always write numbers with Arabic digits (0-9), even in Burmese replies — \
never Burmese numerals (၀-၉).

TOTALS AND COUNTS
- NEVER add, sum, or count numbers yourself. For total stock across sites (or \
a site count), call `summarize_article` and report its total_stock / \
site_count exactly. Per-site tools (get_stock) are ONLY for per-site figures.

USE TOOLS FOR FACTS
- For any article detail, stock level, price, substitute, or count, you MUST \
call the appropriate tool and use its result. Never guess, estimate, or \
invent numbers, prices, or availability.
- If a tool returns nothing, say so plainly rather than fabricating an answer.
- When the user gives an article code, you MUST call `get_article_info` (or \
`summarize_article`) BEFORE answering — never describe or name a product from a \
code using prior knowledge. If the tool returns nothing for that code, say the \
code was not found; do NOT guess the product.
- Answer strictly from tool results. If no tool returned data, say you could not \
find it rather than filling the answer from general knowledge.

RESPONSE STYLE
- Be concise and fast. Lead with the product name, article code, stock, and \
price. Give at most a one-line indication per product. Do NOT write \
multi-section clinical essays, dosing schedules, lifestyle guidance, or long \
background. When listing products, cap at the top 3-5 most relevant unless the \
user asks for more.
- EXCEPTION: when the user asks for substitutes, alternatives, or similar/equivalent \
products, list ALL of them the tool returned (do not cap at 5) — the user needs the \
full set to choose.

FORMATTING
- Your reply is rendered as Markdown. Use it.
- When a tool returns MORE THAN TWO rows that share the same columns — stock \
across branches, a price comparison, a substitute list, a top-N ranking — present \
them as a Markdown pipe table, not as prose sentences or bullets. One row per \
record. Put the identifying column (branch or product) first, numbers after.
- Right-hand number columns: no thousands separators inside the table, and no \
units in the cells — put the unit in the header (e.g. "Stock (units)", "Price (MMK)").
- Lead with ONE short sentence answering the question directly, then the table. \
Do not repeat in prose what the table already shows.
- Two rows or fewer: stay in prose, a table is noise.
- Bold the product name on first mention. Write article codes bare — the UI turns \
any 10-14 digit run into a clickable chip on its own. Do NOT wrap them in \
backticks: product names in this catalog use a backtick as an apostrophe \
("PARACAP PARACETAMOL 10`S"), and a second backtick on the same line pairs with it.
- A table never replaces the tools. Every cell is a value a tool returned — never \
add a row, a column, or a total that no tool gave you.

SCOPE
- You are a pharmacy inventory/catalog assistant, not a doctor. Surface the \
products the pharmacy stocks for the user's need (name, code, stock, price, \
brief indication). Do NOT give clinical dosing instructions, treatment plans, \
or medical advice — for how to take a medicine or diagnosis, defer to a \
licensed pharmacist. Keep the existing safety disclaimer sentence.

SITE SCOPING
- Site codes look like "20005-CCYK" (a numeric prefix and a short letter code). \
When the user names a branch by anything other than its exact code, FIRST call \
`list_sites` to find the matching site_code, then pass that EXACT code to the \
stock/price tool. Never pass a vague partial like "200" — it is ambiguous.
- If `list_sites` returns no clear match for the branch the user named, tell \
them you can't find that branch and show the available site codes. Do NOT report \
another branch's numbers as if they were the requested branch.
- When no site is given, answer across all sites (or the single site you are \
scoped to). When the user names a site, report ONLY that site's figures.
- If the user's own store has no stock (or is running low) of an article, use \
`find_at_other_stores` to offer OTHER branches that have it — quantities only, \
no prices. Make it explicit that those quantities belong to other branches, \
not the user's own store.

CITATIONS
- Ground every factual answer in the tool results. Cite the source so it is \
verifiable: include the article code, and the site code for stock/price, and \
note the origin when relevant ("from inventory", "from the catalog", "from the \
drug knowledge graph", "by closest meaning"). Keep citations brief and inline.

SAFETY
- End every answer that contains stock or price information with this exact \
sentence (in the user's language): \
"Please consult a licensed pharmacist before use."
"""


# Selectable chat models for the in-app A/B picker. Only ids in this allowlist
# may be requested per-message (anything else falls back to the configured
# default). Prices are USD per 1M tokens (in / out) from OpenRouter.
SELECTABLE_MODELS = [
    {
        "id": "google/gemini-2.5-flash-lite",
        "name": "Gemini 2.5 Flash Lite",
        "price_in": 0.10,
        "price_out": 0.40,
        "note": "cheapest · fastest",
    },
    {
        "id": "google/gemini-2.5-flash",
        "name": "Gemini 2.5 Flash",
        "price_in": 0.30,
        "price_out": 2.50,
        "note": "balanced",
    },
    {
        "id": "google/gemini-3.5-flash",
        "name": "Gemini 3.5 Flash",
        "price_in": 1.50,
        "price_out": 9.00,
        "note": "newest · strongest flash",
    },
]
ALLOWED_MODEL_IDS = {m["id"] for m in SELECTABLE_MODELS}


# Appended to the system prompt ONLY for the learning-enabled agent. Memory in a
# medical tool must never become a source of clinical facts — it is scoped to
# user preferences, and tool results always override remembered text.
LEARNING_SAFETY_NOTE = """\
MEMORY SAFETY (critical — this is a medical tool):
- Any remembered facts about the user are limited to PREFERENCES ONLY: their default pharmacy site, and how they like answers formatted.
- NEVER remember or infer a preferred reply language. Reply language is decided fresh from each message's own script, never from memory.
- NEVER treat remembered text as a source of drug facts, stock levels, prices, substitutes, dosages, or any clinical information. Those MUST come from a fresh tool call on EVERY answer, with citations.
- If memory and a tool result disagree, the tool result always wins.
"""


def _async_dsn(postgres_url: str) -> str:
    """Return ``postgres_url`` using the asyncpg driver.

    If the DSN does not already target asyncpg, swap the leading
    ``postgresql://`` scheme for ``postgresql+asyncpg://``. Otherwise return it
    unchanged.
    """

    if "+asyncpg" in postgres_url:
        return postgres_url
    if postgres_url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + postgres_url[len("postgresql://"):]
    return postgres_url


# Cached singleton for the Agno learning DB. Built once (it owns a DB engine), so
# we use a module-level guard rather than lru_cache. ``None`` means construction
# failed and callers should fall back to the non-learning agent.
_LEARNING_DB = None
_LEARNING_DB_BUILT = False


def _get_learning_db():
    """Return a cached :class:`AsyncPostgresDb`, or ``None`` if it can't be built.

    Imports ``AsyncPostgresDb`` lazily so a missing/renamed agno symbol can't
    crash module import for the non-learning path.
    """

    global _LEARNING_DB, _LEARNING_DB_BUILT
    if _LEARNING_DB_BUILT:
        return _LEARNING_DB

    _LEARNING_DB_BUILT = True
    try:
        from agno.db.async_postgres import AsyncPostgresDb

        settings = get_settings()
        _LEARNING_DB = AsyncPostgresDb(
            db_url=_async_dsn(settings.postgres_url),
            db_schema="public",
            create_schema=True,
        )
    except Exception:   # noqa: BLE001 — any failure must degrade gracefully
        logger.exception("Failed to build Agno learning DB; learning disabled")
        _LEARNING_DB = None
    return _LEARNING_DB


def build_agent(model_id: str | None = None) -> Agent:
    """Construct and return a configured Agno agent for ``model_id``.

    Wires an OpenRouter chat model (``model_id`` or the configured default),
    the pharmacy tools, and :data:`BILINGUAL_SYSTEM_PROMPT`.
    """

    settings = get_settings()
    answer_id = model_id or settings.openrouter_model
    model = OpenRouter(
        id=answer_id,
        api_key=settings.openrouter_api_key,
        max_tokens=2048,   # room for a full substitute list (see RESPONSE STYLE exception); still caps essays
    )
    extra: dict = {}
    # Router/answer split: the cheap router_model drives the tool-selection loop,
    # while the requested (stronger) model phrases the final answer via Agno's
    # first-class output_model. Agno relabels the router's own content as an
    # IntermediateRunContentEvent, so only the answer model's prose streams out
    # (agno/agent/_run.py stream path; the system_message — BILINGUAL_SYSTEM_PROMPT
    # — is preserved for output_model because output_model_prompt is left unset).
    if getattr(settings, "router_split_enabled", False) and settings.router_model:
        model = OpenRouter(
            id=settings.router_model,
            api_key=settings.openrouter_api_key,
            max_tokens=2048,
        )
        extra["output_model"] = OpenRouter(
            id=answer_id,
            api_key=settings.openrouter_api_key,
            max_tokens=2048,
        )
    return Agent(
        model=model,
        tools=TOOLS,
        system_message=BILINGUAL_SYSTEM_PROMPT,
        markdown=True,
        **extra,
    )


def build_learning_agent(model_id: str | None = None) -> Agent:
    """Construct a learning-enabled Agno agent for ``model_id``.

    Same wiring as :func:`build_agent`, plus an Agno :class:`LearningMachine`
    (preferences-only user memory, session context, decision log) backed by a
    shared :class:`AsyncPostgresDb`, and the :data:`LEARNING_SAFETY_NOTE`
    appended to the system prompt.

    Degrades gracefully: if the DB can't be built or any error occurs while
    wiring the learning machine, falls back to the plain
    :func:`build_agent` so the app never crashes. ``LearningMachine`` is
    imported lazily so a missing/renamed agno symbol can't break the
    non-learning path.
    """

    settings = get_settings()
    db = _get_learning_db()
    if db is None:
        return build_agent(model_id)

    try:
        from agno.learn.machine import LearningMachine

        model = OpenRouter(
            id=model_id or settings.openrouter_model,
            api_key=settings.openrouter_api_key,
            max_tokens=2048,   # room for a full substitute list (see RESPONSE STYLE exception); still caps essays
        )
        lm = LearningMachine(
            db=db,
            model=OpenRouter(
                id=settings.learning_model,
                api_key=settings.openrouter_api_key,
                max_tokens=1500,   # cheap extraction model
            ),
            user_memory=True,
            session_context=True,
            decision_log=True,
        )
        return Agent(
            model=model,
            tools=TOOLS,
            system_message=BILINGUAL_SYSTEM_PROMPT + "\n\n" + LEARNING_SAFETY_NOTE,
            markdown=True,
            db=db,
            learning=lm,
            add_history_to_context=True,
            num_history_runs=3,
        )
    except Exception:   # noqa: BLE001 — any failure must degrade to the plain agent
        logger.exception("Failed to build learning agent; falling back to plain agent")
        return build_agent(model_id)


@lru_cache(maxsize=8)
def _agent_for(model_id: str) -> Agent:
    return build_agent(model_id)


@lru_cache(maxsize=8)
def _learning_agent_for(model_id: str) -> Agent:
    return build_learning_agent(model_id)


def get_agent(model_id: str | None = None) -> Agent:
    """Return a cached agent for the requested model.

    Unknown / empty ids fall back to the configured default model. Agents are
    cached per model id so switching is cheap. When ``settings.learning_enabled``
    is true, returns the learning-enabled agent; otherwise the plain agent.
    """

    settings = get_settings()
    chosen = model_id if model_id in ALLOWED_MODEL_IDS else settings.openrouter_model
    if settings.learning_enabled:
        return _learning_agent_for(chosen)
    return _agent_for(chosen)


__all__ = [
    "BILINGUAL_SYSTEM_PROMPT",
    "TOOLS",
    "SELECTABLE_MODELS",
    "ALLOWED_MODEL_IDS",
    "build_agent",
    "build_learning_agent",
    "get_agent",
]
