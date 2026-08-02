"""Put the safety line on clinical answers, and only on clinical answers.

The prompt already says this. It is not enough. Measured 2026-08-02 on the
running stack, the same question class gave different results run to run:

    "what is the price of ROYAL-D 25G"     -> disclaimer   (wrong)
    "ROYAL-D 25G ဈေးဘယ်လောက်လဲ"              -> none        (right)
    "how much is it" (as a follow-up)      -> none        (right)

Whether a medical warning appears is not a stylistic choice that may vary with
sampling. Either the answer tells someone how to take a medicine — in which
case the line must be there — or it reports a number off a shelf, in which case
it must not, because a warning printed on every reply is one nobody reads by the
end of the day. So the decision is made here, deterministically, after the model
has written the answer.

The rule:

* the answer carries CLINICAL content (a dose, how to take it, what it treats,
  a side effect, a suggestion for a symptom) -> ensure exactly one line, last.
* the answer is stock, price, a code, a brand name or a category -> remove it.

Detection is on the text, which is imperfect and deliberately biased: an
ambiguous answer keeps the line. A false positive costs one redundant sentence;
a false negative means dosing instructions went out with no warning attached.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

DISCLAIMER_EN = "Please consult a licensed pharmacist before use."
DISCLAIMER_MM = "အသုံးမပြုမီ လိုင်စင်ရ ဆေးဝါးကျွမ်းကျင်သူနှင့် တိုင်ပင်ဆွေးနွေးပါ။"

# Burmese block, used only to pick which sentence to write.
_MM = re.compile(r"[က-႟]")

# Every wording of the line we have shipped, so an existing one can be found and
# removed or moved rather than duplicated. Kept loose on purpose: the model
# paraphrases, and a paraphrase we fail to spot becomes a second copy.
_EXISTING = re.compile(
    r"[^.။\n]*(?:"
    r"consult\s+(?:a\s+)?(?:licensed|qualified|registered)?\s*pharmacist"
    r"|consult\s+(?:a\s+)?(?:healthcare|health\s*care)\s+professional"
    r"|ဆေးဝါးကျွမ်းကျင်သူ|ဆေးဝါးပညာရှင်|လိုင်စင်ရ"
    r")[^.။\n]*[.။]?",
    re.IGNORECASE,
)

# --- clinical markers -------------------------------------------------------
#
# NOT bare units. "MG" appears in almost every brand name in this catalog
# ("BIOGESIC 500MG 10`S"), so matching "mg" would mark every stock answer
# clinical and put the warning back on all of them — the exact bug being fixed.
# These are phrases about USE.

_CLINICAL = re.compile(
    r"""(?:
        # how to take it
          \b(?:once|twice|thrice|three\s+times|four\s+times)\s+(?:a\s+)?(?:daily|day)\b
        | \b(?:per|a)\s+day\b
        | \bat\s+bed\s*time\b | \bbefore\s+bed\b | \bnightly\b
        | \b(?:before|after|with)\s+(?:meals?|food)\b
        | \bon\s+an\s+empty\s+stomach\b
        | \b(?:take|takes|taken|taking|apply|applied|swallow|inhale|inject)\b
        | \b(?:puffs?|drops?|spoonfuls?|sachets?)\b
        | \bdos(?:e|age|ing)\b
        | \bas\s+(?:prescribed|directed)\b
        # what it is for
        | \bused\s+(?:to|for)\b | \bindicated\s+for\b | \btreats?\b | \btreatment\s+of\b
        | \breliev(?:e|es|ing)\b | \bfor\s+the\s+relief\b
        | \bindication\b
        # what it might do to you
        | \bside\s*[- ]?effects?\b | \badverse\b | \bcontraindicat\w*
        | \ballerg(?:y|ic)\b | \bwarnings?\b
        # Burmese: dose / use / treat / side effect
        | သောက် | လိမ်း | ထိုး | သုံးစွဲ | အသုံးပြု
        | ကုသ | ရောဂါ | ဆေးပမာဏ | ဘေးထွက်
        | တစ်နေ့ | တစ်ကြိမ် | နှစ်ကြိမ်
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Phrases that look clinical but are not, checked before the markers above.
# "Prescription only" is a shelf fact — which cabinet the box lives in — not an
# instruction for taking anything.
_NOT_CLINICAL = re.compile(
    r"^\W*(?:prescription[\s-]*only|otc|over[\s-]the[\s-]counter)\W*$",
    re.IGNORECASE,
)


def has_disclaimer(text: str) -> bool:
    """True when the answer already carries some form of the safety line."""

    return bool(_EXISTING.search(text or ""))


def strip_disclaimer(text: str) -> str:
    """Remove every copy of the safety line, however it was worded."""

    out = _EXISTING.sub("", text or "")
    # Collapse the whitespace the removal leaves behind, without joining
    # paragraphs that were deliberately separate.
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def is_clinical(text: str) -> bool:
    """True when the answer tells someone how to use a medicine or what it does.

    Product names are removed first: a brand like "AIR-X DROP 15ML" contains
    "DROP", and "PARACAP PARACETAMOL 10`S" is a name, not an instruction.
    """

    if not text:
        return False
    body = strip_disclaimer(text)
    # Bold runs are product names in this app's formatting; drop them so a
    # name cannot supply the marker that makes an answer look clinical.
    body = re.sub(r"\*\*[^*]{0,80}\*\*", " ", body)
    # Drop bare article codes for the same reason.
    body = re.sub(r"\b\d{10,14}\b", " ", body)
    if _NOT_CLINICAL.match(body.strip()):
        return False
    return bool(_CLINICAL.search(body))


def apply_policy(text: str, force: Optional[bool] = None) -> str:
    """Return the answer with the safety line present iff it belongs there.

    ``force`` overrides the detection (True = always attach, False = always
    remove) for callers that already know — the fast path only ever answers
    stock and price, so it can pass False without paying for the regex.
    """

    if not text or not text.strip():
        return text

    clinical = is_clinical(text) if force is None else force
    body = strip_disclaimer(text)

    # Stripping left nothing: the whole answer WAS the safety line. Removing it
    # would hand the user an empty bubble, which reads as a broken widget. Keep
    # what the model wrote — one redundant sentence beats no answer at all.
    if not body.strip():
        return text.strip()
    if not clinical:
        return body

    line = DISCLAIMER_MM if _MM.search(body) else DISCLAIMER_EN
    sep = "\n\n" if "\n" in body else " "
    return f"{body}{sep}{line}"
