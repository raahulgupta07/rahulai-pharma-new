"""Keep the model's private reasoning out of the customer's chat window.

The 2026-07-28 field reports include five screenshots where the agent streamed
its own deliberation to a pharmacist as if it were the answer — tool names,
empty-result notation, and the system prompt quoted back verbatim:

    thought We are in store 20015-CCGV. The search tools (search_by_meaning,
    search_by_name) are returning nothing... Let's look at the result of
    top_by_stock. It returned items with brand_name being numbers...

    Let me double-check the prompt instructions: "Reply ONLY in Burmese."
    "If a tool returns nothing, say so plainly rather than fabricating..."

Agno separates reasoning from content (``RunContentEvent.reasoning_content``),
so this is not an event-routing bug — the model emits its thinking as ordinary
content and it flows straight through the SSE ``data:`` frames. It has to be
caught on the text.

Design
------
Filtering a stream means deciding before you have the whole answer. This works
a paragraph at a time: text is buffered until a paragraph is complete, the
paragraph is classified, and only clean paragraphs are released. Latency cost
is one paragraph, not the whole answer.

Precision matters more than recall in both directions here — suppressing a real
answer is as bad as leaking. So the two rules are deliberately narrow:

* a **tool name** anywhere in a paragraph is decisive. No customer-facing
  pharmacy answer contains ``search_by_name``. Same for quoted prompt rules.
* a **deliberation opener** counts only at the start of a paragraph, because
  "Let me know if..." mid-sentence is ordinary prose while "Let me search..."
  opening a paragraph is not.

Legitimate answers that merely report failure ("I searched our catalog but could
not find...") are NOT leaks and must survive — several tickets are complaints
about those answers, and they are handled by prompt work, not by this filter.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

logger = logging.getLogger(__name__)

# Every tool the agent can call. Any of these in customer-visible text means the
# model is narrating its own process. Kept in sync with app/tools.py.
TOOL_NAMES = (
    "search_by_name",
    "search_by_meaning",
    "get_substitutes",
    "get_stock",
    "top_by_stock",
    "filter_by_price",
    "get_article_info",
    "summarize_article",
    "related_drugs",
    "drugs_for_same_condition",
    "find_at_other_stores",
    "list_sites",
)

# Fragments of our own system prompt. If these come back out, the model is
# reading its instructions aloud (seen verbatim in Feedback 5 and 7).
PROMPT_ECHOES = (
    "system prompt",
    "prompt instructions",
    "reply only in",
    "keep numbers as arabic digits",
    "if a tool returns nothing",
    "answer strictly from tool results",
    "safety disclaimer should be",
    # Section headings of BILINGUAL_SYSTEM_PROMPT and the answer-length
    # directive, quoted back verbatim. Seen live on 2026-08-13 through the
    # embed API — the whole answer was the model reciting its own rules:
    #     And ANSWER LENGTH - CRISP rules:
    #     "Answer in ONE short line. Give only the product name, article
    #      code, and the single figure asked for (stock or price)."
    #     Wait, if it's a scope limit:
    # None of the existing markers matched, so it reached the customer AND was
    # cached, then served in 0ms to everyone who asked again.
    "answer length",
    "answer in one short line",
    "response style",
    "search strategy",
    "stock answers — what must be in them",
    "stock answers - what must be in them",
)

# Deliberation openers — decisive only at the start of a paragraph.
_OPENERS = re.compile(
    r"""^\s*(?:
          thought\b
        | step\s*\d+\s*[:.]
        | wait[\s,!.]
        | hold\s+on\b
        | let['’]s\b
        | let\s+me\b
        | hmm+\b
        | actually[\s,]
        | okay[\s,]+so\b
        | first[\s,]+let
        | i\s+should\s+(?:check|search|try|look|call)\b
        | i\s+need\s+to\s+(?:check|search|try|look|call)\b
        | maybe\s+(?:i|we)\s+(?:should|can)\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Drafting scaffolding. Observed live on 2026-08-02 against the repaired
# catalog: the model emitted a numbered self-review outline and a parenthetical
# note about its own word choice, after the Burmese answer body —
#     ... တိုင်ပင်ဆွေးနွေးပါ။" (Using standard Myanmar phrasing).
#     4.  **Final Polish
# None of the paragraph openers above match that, because it begins with a
# digit and with Burmese script. These patterns are matched anywhere in the
# paragraph, since the model interleaves them with real prose.
_DRAFTING = re.compile(
    r"""(?:
        # "4.  **Final Polish" — a numbered outline item, but ONLY when the
        # heading is a drafting word. An earlier version matched any numbered
        # bold item and ate real answers: "1. **STROCAIN** relieves stomach
        # pain" is the correct reply to a multi-part question, and it has the
        # identical shape. Caught by the field-feedback eval, 2026-08-02.
          ^\s*\d+\s*[.)]\s*\*{0,2}\s*
            (?:final|draft|polish|review|verify|check|translate|refine|
               formatting|tone|sanity)\b
        | \*\*\s*(?:final|draft)\s+
            (?:polish|answer|version|reply|check)
        | \(\s*using\s+[^)]{0,40}phrasing  # "(Using standard Myanmar phrasing)"
        | \b(?:final|draft)\s+(?:polish|version)\b
        | \btranslat(?:e|ing|ion)\s+(?:this|that|the\s+above)\b
        | \bnow\s+(?:let\s+me\s+)?(?:write|compose|draft)\b
        # Mid-sentence self-correction: "...(ORANGE)** -> wait, the brand name
        # is ...". The paragraph openers above only fire at a paragraph start,
        # so this slipped through. Deliberately requires a following pronoun or
        # article, because "wait 30 minutes before eating" is a real dosage
        # instruction and must never be mistaken for deliberation.
        | \bwait,\s+(?:the|this|that|i|we|it|actually|no
                     # Widened 2026-08-13: the live leak read "Wait, if it's a
                     # scope limit:" and "Wait, let's keep" — neither matched.
                     # Still requires the comma, so the dosage instruction
                     # "wait 30 minutes before eating" is untouched.
                     |if|but|maybe|perhaps|should|let|do|does|is|are|when|why|how)\b
        | ->\s*wait\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Empty-result notation and tag artifacts that reached the UI ("stextI searched"
# opened an answer in Feedback 14).
_ARTIFACTS = re.compile(r"^\s*(?:s?text|</?\s*(?:text|answer|response)\s*>)", re.IGNORECASE)

# When every paragraph is suppressed the customer must still get something. An
# empty bubble reads as a broken widget, and the honest statement is that the
# lookup did not produce a usable answer — not a fabricated one.
FALLBACK_EN = (
    "I could not produce a reliable answer for that. Please rephrase, or give "
    "the article code or brand name.\n\nPlease consult a licensed pharmacist "
    "before use."
)
FALLBACK_MM = (
    "ဤမေးခွန်းအတွက် တိကျသော အဖြေ မထုတ်ပေးနိုင်ပါ။ ကျေးဇူးပြု၍ ဆေးအမည် သို့မဟုတ် "
    "ကုန်ပစ္စည်းကုဒ် (article code) ဖြင့် ပြန်လည်မေးပါ။\n\nအသုံးမပြုမီ လိုင်စင်ရ "
    "ဆေးဝါးကျွမ်းကျင်သူနှင့် တိုင်ပင်ဆွေးနွေးပါ။"
)

# Burmese occupies U+1000–U+109F; used only to pick the fallback language.
_MM = re.compile(r"[က-႟]")


def looks_like_reasoning(paragraph: str) -> bool:
    """True when this paragraph is the model thinking rather than answering."""

    if not paragraph.strip():
        return False
    low = paragraph.lower()
    if any(t in low for t in TOOL_NAMES):
        return True
    if any(p in low for p in PROMPT_ECHOES):
        return True
    if _DRAFTING.search(paragraph):
        return True
    return bool(_OPENERS.match(paragraph))


def fallback_for(original: str) -> str:
    """Safe reply when filtering removed everything, in the answer's language."""

    return FALLBACK_MM if _MM.search(original or "") else FALLBACK_EN


def strip_artifacts(text: str) -> str:
    """Remove a mis-stripped tag bleeding into the first token."""

    return _ARTIFACTS.sub("", text, count=1).lstrip() if _ARTIFACTS.match(text) else text


class LeakFilter:
    """Streaming paragraph filter. Feed deltas, emit only clean text.

    Usage mirrors the SSE loop::

        f = LeakFilter()
        for delta in stream:
            out = f.feed(delta)
            if out:
                yield sse(out)
        out = f.flush()

    ``f.leaked`` is True when anything was suppressed, so the caller can log or
    count it — a leak that is silently fixed is a leak that silently returns.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._emitted = False
        self.leaked = False
        self.dropped: List[str] = []

    def _release(self, paragraph: str) -> str:
        """Classify one complete paragraph and return what may be emitted."""

        if looks_like_reasoning(paragraph):
            self.leaked = True
            self.dropped.append(paragraph.strip()[:200])
            return ""
        if not self._emitted:
            paragraph = strip_artifacts(paragraph)
            if not paragraph.strip():
                return ""
        self._emitted = True
        return paragraph

    def feed(self, delta: Optional[str]) -> str:
        """Add a streamed chunk; return text that is safe to send now."""

        if not delta:
            return ""
        self._buf += delta
        out = ""
        # Release only whole paragraphs — a partial one cannot be classified.
        while "\n\n" in self._buf:
            para, self._buf = self._buf.split("\n\n", 1)
            cleaned = self._release(para + "\n\n")
            out += cleaned
        return out

    def flush(self) -> str:
        """Classify and release whatever is left at end of stream."""

        rest, self._buf = self._buf, ""
        return self._release(rest) if rest.strip() else ""


def contains_reasoning(text: str) -> bool:
    """True when any paragraph of a finished answer looks like deliberation.

    Exists so a caller can decide NOT to cache. Filtering is best-effort by
    nature — the model can always invent a new shape of self-narration — but a
    leak that gets cached stops being a rare event and becomes a permanent one:
    measured live on 2026-08-13, a single leaked answer was then served from
    Redis in 0.00s to every subsequent asker for the whole TTL.

    So the rule is: if anything about an answer looked like reasoning, that
    answer is disposable. Recomputing it costs one LLM call; caching it costs
    every future customer the same broken reply.
    """

    f = LeakFilter()
    f.feed(text or "")
    f.flush()
    return f.leaked


def filter_answer(text: str, fallback: bool = True) -> str:
    """Filter a complete (non-streamed) answer. Used by the fast path and cache.

    With ``fallback`` (the default), an answer that was entirely reasoning comes
    back as a safe apology rather than an empty bubble.
    """

    f = LeakFilter()
    out = (f.feed(text) + f.flush()).strip()
    if f.leaked:
        logger.warning(
            "suppressed leaked reasoning from an answer: %s", f.dropped[:2],
        )
    if not out and fallback and (text or "").strip():
        return fallback_for(text)
    return out
