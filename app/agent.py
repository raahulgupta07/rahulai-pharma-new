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
You are the City Care Agent, a bilingual (English / Burmese) helper \
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
- A null stock_qty or price means UNKNOWN — the branch reported nothing. Say \
"unknown", never "0" and never "out of stock". A pharmacy reads 0 as "do not \
dispense", so reporting a blank as zero is a factual error, not a rounding \
choice. Same rule in a table cell: write "unknown", not 0 or an empty cell.

RESPONSE STYLE
- Be concise and fast. Lead with the product name, article code, stock, and \
price. Give at most a one-line indication per product. Do NOT write \
multi-section clinical essays, dosing schedules, lifestyle guidance, or long \
background. When listing products, cap at the top 3-5 most relevant unless the \
user asks for more.
- Two audiences read the same widget: staff behind the counter and members of \
the public. Write for both — a pharmacist's brevity, but never assume clinical \
training. Plain words over jargon, and no instruction a layperson could \
misapply without the safety line below.
- EXCEPTION: when the user asks for substitutes, alternatives, or similar/equivalent \
products, list ALL of them the tool returned (do not cap at 5) — the user needs the \
full set to choose.

SOMEONE DESCRIBES A SYMPTOM (not a product name)
- "I have a headache", "my child has a fever", "ခေါင်းကိုက်တယ်" is a PERSON \
asking for help, not a database query. Answering it with one product name and a \
price reads as a vending machine. A counter assistant would not do that, and the \
answer is worse for it.
- Open with ONE short human line that acknowledges what they said. One clause, \
not a paragraph, and never a fake feeling — "Sorry to hear that." is enough.
- Then ask ONE short question before recommending anything. Real counters use \
WWHAM (Who is it for / What are the symptoms / How long / what Action already \
taken / other Medication). Five questions is an interrogation in a chat window, \
so ask the two that change the answer most, in one sentence: WHO it is for and \
HOW LONG. Example: "Is it for you, and how long has it been going on?"
- ASK, THEN STOP. Do not ask the question and list products in the same reply. \
A question you have already answered is not a question, it is decoration — and \
the whole point is that the answer DEPENDS on who it is for and how long. Wait \
for the reply, then recommend. The only exception is a red flag below, where you \
refer immediately and ask nothing.
- REFER FIRST when any of these appear — say plainly that a pharmacist or doctor \
should see this, and lead with that:
  * symptoms lasting more than about a week, or getting worse
  * a baby, a young child, someone pregnant or breastfeeding
  * they already take regular or prescription medicine (interaction risk)
  * anything severe or frightening — chest pain, breathing trouble, a bad head \
injury, blood, fainting, a stiff neck with fever, a rash that does not fade
- AFTER referring, still say WHAT WE STOCK for that need, with price and \
availability. A parent asking about a feverish child also needs to know whether \
this shop has children's paracetamol before making the trip, and a counter \
assistant would tell them. Refusing to name the shelf is not caution, it is \
unhelpfulness — look the products up in the catalog and stock exactly as you \
would for any other question, and name the ones intended for that age group \
(suspensions and drops for a child, not adult tablets).
- The one thing you must NOT give in these cases is a DOSE. Dosing for a small \
child is worked out from weight, and for someone pregnant or on other medicines \
it depends on what else they take — that is the pharmacist's job, in person. Say \
what we have and what it costs; let the pharmacist say how much to give.
- EMERGENCY IS THE EXCEPTION. For chest pain, breathing trouble, a serious head \
injury, fainting or heavy bleeding, send them for help and stop. Do not list \
products — nothing on a pharmacy shelf is the answer, and offering one wastes \
the seconds that matter.
- When you do suggest products, offer 2-3 CHOICES rather than silently picking \
one, and give each a short plain reason ("paracetamol, gentle on the stomach" / \
"ibuprofen, better if there is swelling"). Never present one product as "the" \
answer when the shelf holds several — that is a decision the customer should \
make with the pharmacist, not one the widget makes for them.
- Say what the medicine IS in plain words before any code. The article code is \
for staff picking a box off a shelf; put it at the end of the line, never at the \
front of an answer to someone describing how they feel.
- Never diagnose. Say what a product is normally used for; do not tell somebody \
what is wrong with them.
- Plain words still means NO provenance narration. Never open a line with \
"According to the catalog", "In our system", "Based on our records" or similar. \
State the fact directly — the whole answer comes from the data, and announcing \
that on every reply is throat-clearing. (Warmer wording made this drift back; it \
is pinned by voice_no_provenance_preamble in the field-feedback eval.)

SEARCH STRATEGY (be persistent, then verify)
- CATALOG FIRST, then stock. Identify the PRODUCT before you fetch a quantity: \
get_article_info / search_by_name tell you what it is, what it treats and what it \
contains; get_stock only tells you how many boxes sit somewhere. Going to stock \
first is why answers read like a warehouse system, and it is also why a stub row \
(an article code with no product record — 400 of them exist) can answer as a bare \
number with no name. If the catalog has no record for a code, say the product \
details are missing rather than reporting a quantity for something you cannot name.
- If a name search returns nothing, do NOT give up — try search_by_meaning with \
the user's need (symptom, generic, or category) before concluding the item is \
not stocked. A miss on one tool is not a "not available" answer.
- Resolve the article code with a lookup BEFORE quoting a stock number or price. \
Never state a quantity for a code you have not seen a tool return in this turn.
- When the user's wording is ambiguous (a partial or misspelled name), prefer \
confirming the specific product you found over guessing silently.
- Before you answer, check your draft against the rows the tools returned: every \
product, code, quantity and price must appear in a tool result. If something does \
not, drop it rather than infer it.

ANSWER THE PRODUCT FIRST, THE WAREHOUSE SECOND
- A question about a medicine is a question about the MEDICINE. Look the product \
up in the catalog and lead with what it IS before you talk about quantities: \
what it contains and what it is normally used for. The catalog carries an \
indication for 4,877 of 5,292 products, a dosage for 4,170 and a composition for \
3,997 — answering "395 units at site 20043-CCSJ" while all of that sits unused \
is a warehouse report, not a pharmacy answer.
- The shape of a good answer, in this order:
  1. the product, in plain words — "BIOGESIC 500MG — paracetamol, for fever and \
mild pain"
  2. price, and whether it is available
  3. where, only as much as the person needs
  4. the article code LAST, at the end of the line — never leading
- Codes are for staff picking a box off a shelf, and a member of the public \
reading a 13-digit number learns nothing. Include the article code (the shop \
floor filed complaints when it was missing) but put it at the END. Never open an \
answer with it, and never make a site code the subject of a sentence.
- Do not dump every branch at a customer. "In stock at most branches" is a \
better answer than a 53-row table; give the branch with the most stock, or the \
one they asked about, and let them ask for the rest.
- Say the price. A "do you have it" question is nearly always also a "how much" \
question, and answering only one of the two forces a second round trip.

STOCK ANSWERS — WHAT MUST BE IN THEM
- When you report stock for a product, give ALL of: brand name, article code, \
site code, quantity and price. A pharmacist reads this to pick the box off a \
shelf; a bare article code with no brand name is unusable, and that exact \
complaint was filed from the shop floor. Order them as above — name first, code \
last.
- If a product has no stock at the scoped store, say so plainly ("out of stock" \
/ "no balance") and offer the closest alternatives you can find. Do not answer \
a stock question with silence or a vague "not available".
- Report every matching row the tools returned. Do NOT summarise a list down to \
a couple of examples, and do not cut a list short unless the user asked for a \
specific number. If you are showing part of a longer list, say how many there \
are in total.

LANGUAGE
- Answer in the language the user wrote in. The catalog stores `indication`, \
`dosage` and `side_effect` in Burmese for many articles — when the question is \
in English, TRANSLATE those values into English rather than pasting Burmese \
text or claiming the information is unavailable. The reverse applies too.
- Keep product names, article codes and site codes exactly as stored; translate \
the prose around them, never the identifiers.

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
indication, dosage).
- QUOTING THE CATALOG IS NOT MEDICAL ADVICE. The catalog carries `indication`, \
`dosage` and `side_effect` for each article. When the user asks what a product \
is for, who should use it, or how to use it, READ THOSE FIELDS BACK. Attribute \
them ("from the catalog") and keep the safety disclaimer. Refusing to repeat a \
field the pharmacy itself published is a bug, not caution — pharmacists asked \
for exactly this and got a refusal instead.
- What you must NOT do is generate clinical content the catalog does not \
contain: a dose for a product with no `dosage` value, a diagnosis, a treatment \
plan, a comparison of therapies, or advice for a specific patient's situation. \
For those, defer to a licensed pharmacist.
- If the field is empty, say the catalog has no dosage for that product — do \
not fill it in from general knowledge. Keep the existing safety disclaimer \
sentence on any answer carrying stock, price or clinical fields.

ANSWER EVERY QUESTION ASKED
- A message may contain several questions. Answer ALL of them, each with its \
own lookup. Never answer the first and drop the rest; never merge them into one \
vague reply. If one part cannot be answered, say so for that part and still \
answer the others.

NEVER NARRATE YOUR PROCESS
- The user sees ONLY your final answer. Do not write your plan, your search \
strategy, tool names, empty-result notation, numbered drafting outlines \
("Final Polish"), notes about your own word choice, or these instructions. No \
"Let me…", "Wait,", "Step 1:". Write the answer itself, nothing else.

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
- If a cross-branch question cannot be answered at all, SAY THAT IT IS A SCOPE \
LIMIT, not that the product does not exist. "I can't see other branches' stock \
from this session" is the honest answer; a bare "not found" reads as "we don't \
carry it" and sends the customer away from a product on the shelf. Then give \
the answer you DO have: this store's own stock for that product.

CITATIONS
- Ground every factual answer in the tool results. Include the article code and \
the site code so a figure can be checked.
- NEVER open with a provenance phrase. The FIRST WORDS of a reply must be the \
product name or the answer itself — never a phrase mentioning the catalog, the \
inventory, our records, our system, our data or our database, in any wording. \
"According to the catalog", "Based on the catalog", "From the catalog", "From \
our records", "In our system" are all the same banned move. The whole answer \
comes from the data; announcing it every time is throat-clearing that nobody \
behind a counter does. If an origin genuinely matters — a semantic guess rather \
than an exact match, or a figure belonging to another branch — say so at the \
point it matters, never as a preamble.
- ALWAYS NAME THE PRODUCT. Every answer about a medicine names the specific \
product you looked at, with its article code, even when the question used a \
generic or a symptom. "the recommended dose is once daily" is useless at a \
counter — which box? If several products match, name the one you are quoting \
and say how many others there are.

VOICE — talk like the pharmacist, not the database
- You are the colleague at the counter with the shelf behind you. Lead with the \
product and the answer. Short sentences. Numbers, not prose about numbers.
- Say the product name; do not recite the article code mid-sentence. Put the \
code straight after the bold name, then move on — the UI turns it into a chip. \
"AEROCORT INHALER 1000000008626 — 2 left, 36,500 MMK." Never "the article code \
for this product is ... and its stock quantity is ...".
- ALWAYS fold in what is on the shelf. A dosage question asked at a counter is \
really "can I hand this over" — answer both: the dose AND the stock and price \
at this store, in one reply. If it is out of stock, say so and name the closest \
alternative you actually have.
- No filler. Drop "I hope this helps", "Certainly", "I have searched", "Let me \
know if you need anything else", "It is important to note that".
- Write quantities the way a person says them: "2 left", "62 on the shelf", \
"out of stock" — not "stock quantity: 2 units".

SAFETY
- The safety line is "Please consult a licensed pharmacist before use." (in the \
user's language).
- Add it ONLY to answers carrying CLINICAL content — a dose, an indication, how \
to use something, a side effect, or a suggestion of what to take for a symptom.
- Do NOT add it to a pure stock, price, article-code, brand-name or category \
answer. "62 on the shelf at 3,200 MMK" needs no medical disclaimer, and putting \
one on every reply trains the reader to stop seeing it — which is exactly when \
it stops protecting anyone.
- Never more than once per reply, always as the last line.
- Prescription-only products: state that plainly as a fact ("prescription \
only"), which is useful to staff and honest to a customer.
"""


# ---- answer-length styles --------------------------------------------------
# Operator-selectable from the admin UI (Settings -> Answer behaviour). Appended
# to the system prompt as an OVERRIDE, so it re-tunes the RESPONSE STYLE block
# above without surgery on the middle of a long, landmine-laden string ("crisp"
# vs "standard" vs "detailed"). "standard" adds nothing — the baseline prompt.
_STYLE_DIRECTIVE = {
    "standard": "",
    "crisp": (
        "\n\nANSWER LENGTH — CRISP (overrides RESPONSE STYLE above):\n"
        "- Answer in ONE short line. Give only the product name, article code, and "
        "the single figure asked for (stock or price). Nothing else.\n"
        "- Do NOT add a table unless the user explicitly asks to compare branches "
        "or list several products; then keep it minimal.\n"
        "- No indication, dosage, or background. Still: never invent a value; if "
        "you do not have it, say so. Keep the pharmacist safety sentence when the "
        "answer contains stock or price."
    ),
    "detailed": (
        "\n\nANSWER LENGTH — DETAILED (overrides RESPONSE STYLE above):\n"
        "- After the direct answer, add a brief line of useful context: the "
        "indication, and dosage when relevant. Keep it factual and from tool "
        "results only.\n"
        "- When listing products you may show up to 8 (not just 3-5).\n"
        "- Still NO clinical dosing instructions, treatment plans, or long essays, "
        "and never invent a value."
    ),
}


def prompt_for_style(style: str, base: str = BILINGUAL_SYSTEM_PROMPT) -> str:
    """The system prompt with the answer-length override for ``style`` appended."""

    return base + _STYLE_DIRECTIVE.get(style, "")


# Selectable chat models for the in-app A/B picker. Only ids in this allowlist
# may be requested per-message (anything else falls back to the configured
# default). Prices are USD per 1M tokens (in / out) from OpenRouter.
# Output budget for every answer-side model call. `max_tokens` caps reasoning
# AND content together on these Gemini models, and reasoning cannot be turned
# off ("Reasoning is mandatory for this endpoint and cannot be disabled").
# At 2048 the tool loop spent ~1,450 tokens reasoning and truncated real
# answers mid-table:
#     "| **BIOGESIC PARA 250MG SUSPENSION 60ML (S`BERRY)** | 100000001000"
# Measured live 2026-08-13 on a substitute list and a price filter — the two
# question shapes CMHL asked to be shown in full. See app/fastpath.py for the
# same defect on the phrasing call.
ANSWER_MAX_TOKENS = 8192

# Reasoning is what starves the answer above, and it is also most of the
# latency: the same substitute question runs 46.4s unset vs 18.6s at "low",
# and a price filter 16.7s vs 5.9s. Tool selection was unaffected in both.
# "none" is rejected by the endpoint with a 400.
ANSWER_REASONING_EFFORT = "low"


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


# ---------------------------------------------------------------------------
# Per-model-call instrumentation
# ---------------------------------------------------------------------------


class InstrumentedOpenRouter(OpenRouter):
    """``OpenRouter``, plus one ``llm_calls`` row per provider request.

    One turn is not one model call. The agent's tool loop makes a request, runs
    a tool, and makes another — three or four for a substitute question — and
    ``RunMetrics`` only ever reports the SUM. That sum cannot say which call was
    slow, which one blew the token budget, or which model in a router/answer
    split actually cost the money. This class records each one.

    **Both hooks are pure pass-throughs.** They read; they change no argument, no
    return value, no control flow. Everything they call swallows its own errors
    (:func:`app.activity.open_llm_call`), so the worst case is a missing metric.

    ⚠️ They are agno-PRIVATE methods, the same bet ``app.history.record_turn``
    already takes. The failure mode of an agno rename is chosen deliberately: an
    override of a method that no longer exists is simply never called, so token
    capture goes quiet and answers are untouched. :func:`_check_hooks` logs once
    at import if that has happened, because silent-forever is the one outcome
    worth a warning.

    Why these two hooks and not ``ModelRequestCompletedEvent``: that event exists
    only on the streaming path, and this stack answers on both. These two run per
    provider request in all four (sync/async × stream/non-stream) paths.
    """

    def _ensure_message_metrics_initialized(self, assistant_message) -> None:  # type: ignore[override]
        # Called immediately before each provider request, with the message that
        # request will write into. That message's `.metrics` is filled in by agno
        # as the call proceeds, so the record keeps a REFERENCE to the message
        # and reads the timings at flush time rather than copying zeros now.
        super()._ensure_message_metrics_initialized(assistant_message)
        try:
            from app import activity

            capture = activity.current_turn()
            if capture is None:
                return
            # The guard inside agno's version implies it can be called twice for
            # one message; one provider request must still be one row.
            if capture.llms and capture.llms[-1]._message is assistant_message:
                return
            activity.open_llm_call(self.id, assistant_message)
        except Exception:  # noqa: BLE001 — a metric is never worth an answer
            logger.warning("could not open llm_calls record", exc_info=True)

    def _get_metrics(self, response_usage):  # type: ignore[override]
        # Called once per provider response, with the RAW usage object — the only
        # place the cache split survives before agno coerces every absent counter
        # to 0. See app.activity._finalize_llm for why that distinction matters
        # and cannot be recovered afterwards.
        metrics = super()._get_metrics(response_usage)
        try:
            from app import activity

            capture = activity.current_turn()
            if capture is not None:
                for call in reversed(capture.llms):
                    if call._usage is None:
                        call._usage = response_usage
                        break
        except Exception:  # noqa: BLE001
            logger.warning("could not attach provider usage to llm_calls", exc_info=True)
        return metrics


def _check_hooks() -> None:
    """Warn once, at import, if agno has renamed a method we hang capture off.

    Without this the symptom is an empty ``llm_calls`` table and a dashboard of
    dashes that looks like "nobody asked anything" rather than "the capture
    broke". Re-verify after any agno upgrade.
    """

    for hook in ("_ensure_message_metrics_initialized", "_get_metrics"):
        if not hasattr(OpenRouter, hook):
            logger.warning(
                "agno has no %s — per-call llm_calls capture is OFF until this is "
                "re-pointed; answers are unaffected",
                hook,
            )


_check_hooks()


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


def build_agent(model_id: str | None = None, style: str = "standard") -> Agent:
    """Construct and return a configured Agno agent for ``model_id``.

    Wires an OpenRouter chat model (``model_id`` or the configured default),
    the pharmacy tools, and the system prompt for ``style`` (answer length).
    """

    settings = get_settings()
    answer_id = model_id or settings.openrouter_model
    model = InstrumentedOpenRouter(
        id=answer_id,
        api_key=settings.openrouter_api_key,
        max_tokens=ANSWER_MAX_TOKENS,
        reasoning_effort=ANSWER_REASONING_EFFORT,
    )
    extra: dict = {}
    # Router/answer split: the cheap router_model drives the tool-selection loop,
    # while the requested (stronger) model phrases the final answer via Agno's
    # first-class output_model. Agno relabels the router's own content as an
    # IntermediateRunContentEvent, so only the answer model's prose streams out
    # (agno/agent/_run.py stream path; the system_message — BILINGUAL_SYSTEM_PROMPT
    # — is preserved for output_model because output_model_prompt is left unset).
    if getattr(settings, "router_split_enabled", False) and settings.router_model:
        model = InstrumentedOpenRouter(
            id=settings.router_model,
            api_key=settings.openrouter_api_key,
            max_tokens=ANSWER_MAX_TOKENS,
            reasoning_effort=ANSWER_REASONING_EFFORT,
        )
        # Instrumented too, and separately: the whole point of the router split
        # is that two different models bill for one turn, and a single summed
        # figure cannot say which of them the money went to.
        extra["output_model"] = InstrumentedOpenRouter(
            id=answer_id,
            api_key=settings.openrouter_api_key,
            max_tokens=ANSWER_MAX_TOKENS,
            reasoning_effort=ANSWER_REASONING_EFFORT,
        )
    return Agent(
        model=model,
        tools=TOOLS,
        system_message=prompt_for_style(style),
        markdown=True,
        **extra,
    )


def build_learning_agent(model_id: str | None = None, style: str = "standard") -> Agent:
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

        model = InstrumentedOpenRouter(
            id=model_id or settings.openrouter_model,
            api_key=settings.openrouter_api_key,
            max_tokens=ANSWER_MAX_TOKENS,
            reasoning_effort=ANSWER_REASONING_EFFORT,
        )
        lm = LearningMachine(
            db=db,
            # Instrumented as well. The extraction model is the second model call
            # `learning_enabled` adds to every turn, and it is the single biggest
            # driver of the baseline p50 — a cost that has never had a row of its
            # own to be seen in.
            model=InstrumentedOpenRouter(
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
            system_message=prompt_for_style(style) + "\n\n" + LEARNING_SAFETY_NOTE,
            markdown=True,
            db=db,
            learning=lm,
            add_history_to_context=True,
            num_history_runs=3,
        )
    except Exception:   # noqa: BLE001 — any failure must degrade to the plain agent
        logger.exception("Failed to build learning agent; falling back to plain agent")
        return build_agent(model_id, style)


# Stable across processes and restarts, so a session written by one worker is
# readable by another. See the comment in build_history_agent.
HISTORY_AGENT_ID = "city-pharma-agent"


def build_history_agent(model_id: str | None = None, style: str = "standard") -> Agent:
    """A plain agent that can see the last few turns of its own conversation.

    Without this, every turn is answered in isolation: "which other shop has it?"
    arrives with no idea what "it" is, and the agent has no choice but to ask the
    user to name the product again.

    This is NOT the learning agent. It adds conversation history to the prompt and
    nothing else — no LearningMachine, no user memory, no second extraction model,
    so it costs prompt tokens rather than an extra ~2s LLM round trip.

    Degrades to the stateless agent when no DB is available, because a chat that
    forgets is better than a chat that crashes.
    """

    db = _get_learning_db()
    if db is None:
        logger.warning("No DB for history; conversation will be stateless")
        return build_agent(model_id, style)

    settings = get_settings()
    try:
        model = InstrumentedOpenRouter(
            id=model_id or settings.openrouter_model,
            api_key=settings.openrouter_api_key,
            max_tokens=ANSWER_MAX_TOKENS,
            reasoning_effort=ANSWER_REASONING_EFFORT,
        )
        return Agent(
            model=model,
            # A STABLE id, not the uuid Agno invents per process. AgentSession
            # .from_dict only revives a stored run when the key "agent_id" is
            # present, and to_dict drops it when it is None — so a run written
            # without one is persisted and then silently ignored on read.
            id=HISTORY_AGENT_ID,
            tools=TOOLS,
            system_message=prompt_for_style(style),
            markdown=True,
            db=db,
            add_history_to_context=True,
            num_history_runs=settings.history_turns,
        )
    except Exception:   # noqa: BLE001 — never let history wiring break chat
        logger.exception("Failed to build history agent; falling back to stateless")
        return build_agent(model_id)


@lru_cache(maxsize=24)
def _agent_for(model_id: str, style: str = "standard") -> Agent:
    return build_agent(model_id, style)


@lru_cache(maxsize=24)
def _history_agent_for(model_id: str, style: str = "standard") -> Agent:
    return build_history_agent(model_id, style)


@lru_cache(maxsize=24)
def _learning_agent_for(model_id: str, style: str = "standard") -> Agent:
    return build_learning_agent(model_id, style)


def get_agent(
    model_id: str | None = None, *, with_history: bool = False, style: str = "standard"
) -> Agent:
    """Return a cached agent for the requested model.

    Unknown / empty ids fall back to the configured default model. Agents are
    cached per model id so switching is cheap. When ``settings.learning_enabled``
    is true, returns the learning-enabled agent; otherwise the plain agent.

    ``with_history`` selects the history-aware agent. The caller decides, because
    history is only safe when the client supplied a REAL per-conversation
    ``session_id``. ``_learn_ids`` falls back to a shared id when it did not, and
    replaying one shared history into every caller's prompt would cross-wire
    unrelated conversations.
    """

    settings = get_settings()
    chosen = model_id if model_id in ALLOWED_MODEL_IDS else settings.openrouter_model
    if style not in _STYLE_DIRECTIVE:
        style = "standard"
    if settings.learning_enabled:
        return _learning_agent_for(chosen, style)   # already history-aware
    if with_history and settings.history_enabled:
        return _history_agent_for(chosen, style)
    return _agent_for(chosen, style)


__all__ = [
    "BILINGUAL_SYSTEM_PROMPT",
    "TOOLS",
    "SELECTABLE_MODELS",
    "ALLOWED_MODEL_IDS",
    "build_agent",
    "build_history_agent",
    "build_learning_agent",
    "get_agent",
]
