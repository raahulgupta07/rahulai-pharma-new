"""Fast path — deterministic intent detection + one SQL query + one phrasing call.

The full agent spends 2-3 sequential LLM calls per question (pick a tool, wait
for it, phrase the result). For the two hottest intents that is wasteful: the
tool is obvious. This module resolves the drug with no LLM (see
:mod:`app.resolver`), runs the ONE corresponding SQL query with the exact
semantics of :mod:`app.tools`, and returns structured facts. The single LLM
call that phrases those facts lives in :mod:`app.api`.

Two intents only, matched conservatively in English and Burmese:

    HOT_HAVE  — "do I have X" / "is X in stock" / "X ရှိလား"
    HOT_WHERE — "who else has X" / "where can I find X" / "ဘယ်ဆိုင်မှာ X ရှိလဲ"

Anything not confidently one of these two returns ``None`` and falls through to
the normal agent. False negatives are fine; false positives are not — a wrong
fast-path answer in a pharmacy is worse than a slow one, so an unresolvable
mention also falls through.

Store scope is enforced exactly as in :mod:`app.tools`: this module sets the
``_STORE_SCOPE`` contextvar and calls the real ``get_stock`` /
``find_at_other_stores`` tools, so a scoped session can never read another
branch's rows.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from app import tools
from app.resolver import Resolution, resolve

# Intent labels (also used as the SSE step label so the UI trace names the work).
HOT_HAVE = "get_stock"
HOT_WHERE = "find_at_other_stores"

# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

# English patterns, WHERE checked before HAVE (a "where" phrase may contain
# "has"). Each pattern captures the drug mention. The ``(?!to\b)`` guard keeps
# "do I have to take it with food" from being read as a stock question.
_EN_WHERE = [
    re.compile(r"\bwho\s+else\s+(?:has|have|stocks?|carr(?:y|ies)|sells?)\s+(.+)", re.I),
    re.compile(r"\bwhere(?:\s+else)?\s+can\s+i\s+(?:find|buy|get)\s+(.+)", re.I),
    re.compile(r"\bwhere(?:\s+else)?\s+(?:is|are)\s+(.+)", re.I),
    re.compile(r"\bwhich\s+(?:branch|store|shop|site|outlet)s?\s+(?:has|have|stocks?|carr(?:y|ies))\s+(.+)", re.I),
    re.compile(r"\b(.+?)\s+at\s+(?:other|another|which)\s+(?:store|branch|shop|site|outlet)s?", re.I),
]
_EN_HAVE = [
    re.compile(r"\bdo\s+(?:i|we|you)\s+(?:have|stock|carry)\s+(?!to\b)(.+)", re.I),
    re.compile(r"\b(?:is|are)\s+(?:there\s+)?(?:any\s+)?(.+?)\s+in\s+stock", re.I),
    re.compile(r"\b(?:have|got)\s+(?:any\s+)?(.+?)\s+in\s+stock", re.I),
    re.compile(r"\b(?:in\s+)?stock\s+(?:of|for)\s+(.+)", re.I),
    re.compile(r"\b(.+?)\s+in\s+stock\b", re.I),
]

# Burmese cues (script has no spaces around these, so match as substrings).
_MY_Q = ("လား", "လဲ", "သလား", "သနည်း")       # question particles
_MY_HAVE = "ရှိ"                              # have / exist
_MY_WHERE = ("ဘယ်", "ဘယ်ဆိုင်", "ဘယ်နေရာ")     # where / which
_MY_OTHER = "တခြား"                           # other
# Tokens stripped to leave the (usually English-typed) drug mention behind.
_MY_STRIP = (
    "ဘယ်ဆိုင်မှာ", "ဘယ်ဆိုင်", "ဘယ်နေရာမှာ", "ဘယ်နေရာ", "ဘယ်မှာ", "ဘယ်",
    "တခြားဆိုင်မှာ", "တခြားဆိုင်", "တခြား", "လက်ကျန်", "ရှိသေး", "ရှိ",
    "ရနိုင်", "ဆိုင်", "မှာ", "သလား", "သနည်း", "လား", "လဲ",
)

_TRAILING = re.compile(
    r"\b(in\s+stock|available|stocked|please|right\s+now|now|anymore|any\s+more|left)\b",
    re.I,
)
_LEADING = re.compile(r"^(the|any|some|a|an)\s+", re.I)


def _clean_mention(text: str) -> str:
    """Strip filler, articles and punctuation from a captured drug mention."""

    text = _TRAILING.sub(" ", text)
    text = _LEADING.sub("", text.strip())
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t?.!,;:'\"()")


def _detect_english(message: str) -> Optional[Tuple[str, str]]:
    """Return ``(intent, mention)`` for an English hot question, else ``None``."""

    for pat in _EN_WHERE:
        m = pat.search(message)
        if m:
            return HOT_WHERE, _clean_mention(m.group(1))
    for pat in _EN_HAVE:
        m = pat.search(message)
        if m:
            return HOT_HAVE, _clean_mention(m.group(1))
    return None


def _detect_burmese(message: str) -> Optional[Tuple[str, str]]:
    """Return ``(intent, mention)`` for a Burmese hot question, else ``None``."""

    has_q = any(qp in message for qp in _MY_Q)
    where_cue = any(w in message for w in _MY_WHERE)
    if where_cue and (_MY_HAVE in message or _MY_OTHER in message):
        intent = HOT_WHERE
    elif _MY_HAVE in message and has_q:
        intent = HOT_HAVE
    else:
        return None

    mention = message
    for tok in _MY_STRIP:
        mention = mention.replace(tok, " ")
    return intent, _clean_mention(mention)


def detect_intent(message: str) -> Optional[Tuple[str, str]]:
    """Classify ``message`` as one of the two hot intents, or ``None``.

    Returns ``(intent, mention)`` where ``intent`` is :data:`HOT_HAVE` or
    :data:`HOT_WHERE` and ``mention`` is the extracted drug text. Returns
    ``None`` (fall through to the agent) when the message is not confidently a
    hot question or no usable mention could be extracted.
    """

    if not message:
        return None
    hit = _detect_burmese(message) if any("က" <= ch <= "႟" for ch in message) else _detect_english(message)
    if hit is None:
        return None
    intent, mention = hit
    if len(mention) < 2:
        return None
    return intent, mention


# ---------------------------------------------------------------------------
# Single-query answering
# ---------------------------------------------------------------------------


async def answer(message: str, store_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Resolve one hot question and return structured facts, or ``None``.

    ``None`` means "not a fast-path question" — either the intent was not one of
    the two, or the mention could not be resolved to a real article. Callers
    fall through to the full agent in that case.

    On a hit the drug is resolved with no LLM and the ONE matching tool query is
    run under ``store_id`` scope (identical semantics to :mod:`app.tools`,
    including ``_site_clause`` and ``NULLS LAST``). ``stock_qty`` is passed
    through untouched — ``None`` means UNKNOWN, never zero.
    """

    detected = detect_intent(message)
    if detected is None:
        return None
    intent, mention = detected

    token = tools.set_store_scope(store_id)
    try:
        res = await resolve(mention)
        if res.status is Resolution.NOT_FOUND:
            return None

        facts: Dict[str, Any] = {
            "intent": intent,
            "tool": intent,
            "mention": mention,
            "store_id": store_id,
            "resolution": res.status.value,
        }

        if res.status is Resolution.AMBIGUOUS:
            candidates = [
                {"article_code": c.article_code, "brand_name": c.brand_name,
                 "generic_name": c.generic_name}
                for c in res.candidates
            ]
            facts["candidates"] = candidates
            facts["rows"] = candidates
            return facts

        top = res.candidates[0]
        facts["article_code"] = top.article_code
        facts["brand_name"] = top.brand_name
        facts["generic_name"] = top.generic_name
        if intent == HOT_HAVE:
            facts["rows"] = await tools.get_stock(top.article_code)
        else:
            facts["rows"] = await tools.find_at_other_stores(top.article_code)
        return facts
    finally:
        tools.reset_store_scope(token)


# ---------------------------------------------------------------------------
# Phrasing (single LLM call — no tools, cannot invent a number)
# ---------------------------------------------------------------------------

# The phrasing model is given the facts as data and may only restate them. It
# has NO tools, so it cannot fetch or fabricate a figure. Mirrors the relevant
# rules of BILINGUAL_SYSTEM_PROMPT (literal values, safety sentence).
PHRASING_SYSTEM_PROMPT = """\
You are the City Pharma Assistant. You will be given a user question and a FACTS \
block (JSON) that was already retrieved from the pharmacy database. Phrase a \
short answer to the user from those FACTS ONLY.

RULES
- Use ONLY the numbers, codes, names and quantities in FACTS. NEVER invent, \
add, sum, estimate, or change a value. You have no tools; you cannot look \
anything up.
- Keep article codes, prices, quantities and units exactly as given.
- Always write numbers with Arabic digits (0-9), even in Burmese.
- A stock_qty of null means the quantity is UNKNOWN — say "unknown", never "0".
- Obey the bracketed language directive at the top of the message absolutely: \
reply entirely in that language.
- resolution="AMBIGUOUS": the mention matched several products. Do NOT pick \
one. Briefly list the candidate brand names + article codes and ask the user \
which they mean.
- For "find_at_other_stores" rows, the quantities belong to OTHER branches, not \
the user's own store — say so.
- Be concise: lead with the product name, article code, and stock. No clinical \
essays or dosing advice.
- End every answer that contains stock or price information with this exact \
sentence, in the user's language: \
"Please consult a licensed pharmacist before use."
"""


def build_phrasing_input(scoped_message: str, facts: Dict[str, Any]) -> str:
    """Compose the phrasing prompt: the scoped user message + the FACTS block.

    ``scoped_message`` already carries the deterministic language directive (and
    store context) from :func:`app.api._scoped_message`; the facts are appended
    as JSON so the model restates them without inventing anything.
    """

    return f"{scoped_message}\n\nFACTS (answer from these only):\n{json.dumps(facts, ensure_ascii=False)}"


@lru_cache(maxsize=8)
def get_phrasing_agent(model_id: Optional[str] = None):
    """Return a cached, tool-less Agno agent used only to phrase facts.

    Built with no tools so the phrasing call cannot fetch or fabricate data —
    its sole job is to restate the FACTS in the user's language.
    """

    from agno.agent import Agent
    from agno.models.openrouter import OpenRouter

    from app.agent import ALLOWED_MODEL_IDS
    from app.config import get_settings

    settings = get_settings()
    chosen = model_id if model_id in ALLOWED_MODEL_IDS else settings.openrouter_model
    model = OpenRouter(
        id=chosen,
        api_key=settings.openrouter_api_key,
        max_tokens=1024,
    )
    return Agent(
        model=model,
        tools=[],
        system_message=PHRASING_SYSTEM_PROMPT,
        markdown=True,
    )


def result_rows(facts: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the row list to forward in the SSE ``result`` frame (may be empty)."""

    rows = facts.get("rows") or []
    return rows if isinstance(rows, list) else [rows]


__all__ = [
    "HOT_HAVE",
    "HOT_WHERE",
    "detect_intent",
    "answer",
    "build_phrasing_input",
    "get_phrasing_agent",
    "result_rows",
    "PHRASING_SYSTEM_PROMPT",
]
