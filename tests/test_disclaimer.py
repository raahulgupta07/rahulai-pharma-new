"""The safety line is a binary rule, so it is tested as one.

The prompt asked for this and the model obeyed inconsistently — measured on the
running stack, "what is the price of ROYAL-D 25G" got a medical warning while
the same question in Burmese, and the same question as a follow-up, did not.
Whether a warning appears cannot depend on sampling, so app.disclaimer decides
it after the model has written the answer and these tests pin the decision.

The bias is deliberate: an ambiguous answer KEEPS the line. A false positive
costs one redundant sentence; a false negative sends dosing instructions out
with no warning attached.
"""

from app.disclaimer import (
    DISCLAIMER_EN,
    DISCLAIMER_MM,
    apply_policy,
    has_disclaimer,
    is_clinical,
    strip_disclaimer,
)

# Real answers from the :8091 stack, 2026-08-02.
PRICE_EN = "**ROYAL-D 25G** 1000000015837 — 800 MMK."
STOCK_EN = "**BIOGESIC 500MG 10`S** 1000000131948 — 380 on the shelf, 1700 MMK."
STOCK_MM = "**BIOGESIC 500MG 10`S** (ကုဒ်: 1000000131948) - 380 ခု ရှိပါသည်။"
CODE_ONLY = "**AEROCORT INHALER** → article code 1000000008626."
CATEGORY = "**MOTILIUM-M 10`S** 1000000008745 category is 5102-PRESCRIPTION MEDICINE."

DOSE_EN = ("**AIR-X DROP 15ML** 1000000010002 — 21 on the shelf at 5400 MMK. "
           "The dosage is 0.3 ml to 0.6 ml depending on the child's age.")
USED_FOR = "**STROCAIN** 1000000008737 is used to relieve stomach pain from gastritis."
DOSE_MM = "**AMNOTAC 150** 1000000347875 — တစ်နေ့နှစ်ကြိမ် အစာမစားခင် သောက်ပါ။"


class TestClinicalDetection:
    def test_stock_and_price_are_not_clinical(self):
        for t in (PRICE_EN, STOCK_EN, STOCK_MM, CODE_ONLY, CATEGORY):
            assert not is_clinical(t), t

    def test_dose_and_indication_are_clinical(self):
        for t in (DOSE_EN, USED_FOR, DOSE_MM):
            assert is_clinical(t), t

    def test_a_brand_name_cannot_make_an_answer_clinical(self):
        """The catalog is full of names that read like instructions.

        "AIR-X DROP" contains "drop"; "PARACAP PARACETAMOL 10`S" is a name.
        Matching on the name would mark every stock answer clinical and put the
        warning back on all of them — the bug this module exists to fix.
        """

        assert not is_clinical("**AIR-X DROP 15ML** 1000000010002 — 21 on the shelf.")
        assert not is_clinical("**PARACAP PARACETAMOL 10`S** 1000000024029 — 663 left.")

    def test_mg_in_a_product_name_is_not_a_dose(self):
        assert not is_clinical("**ATORVASTATIN 10MG 10`S** 1000000184035 — 62 on the shelf.")

    def test_prescription_only_alone_is_a_shelf_fact(self):
        """Which cabinet the box lives in, not how to take it."""

        assert not is_clinical("Prescription only")


class TestStripping:
    def test_finds_and_removes_the_english_line(self):
        t = f"{PRICE_EN} {DISCLAIMER_EN}"
        assert has_disclaimer(t)
        assert strip_disclaimer(t) == PRICE_EN

    def test_finds_and_removes_the_burmese_line(self):
        t = f"{STOCK_MM} {DISCLAIMER_MM}"
        assert has_disclaimer(t)
        assert DISCLAIMER_MM not in strip_disclaimer(t)

    def test_removes_a_paraphrase_not_just_the_exact_sentence(self):
        """The model paraphrases; a missed copy becomes a duplicate."""

        t = PRICE_EN + " Please consult a qualified pharmacist before using this."
        assert has_disclaimer(t)
        assert "pharmacist" not in strip_disclaimer(t)

    def test_removes_the_healthcare_professional_variant(self):
        t = DOSE_EN + " For more information, consult a healthcare professional."
        assert "healthcare professional" not in strip_disclaimer(t)

    def test_leaves_an_answer_without_one_untouched(self):
        assert strip_disclaimer(STOCK_EN) == STOCK_EN


class TestPolicy:
    def test_removes_it_from_a_price_answer(self):
        out = apply_policy(f"{PRICE_EN} {DISCLAIMER_EN}")
        assert out == PRICE_EN
        assert not has_disclaimer(out)

    def test_adds_it_to_a_dose_answer_that_lacks_one(self):
        out = apply_policy(DOSE_EN)
        assert out.endswith(DISCLAIMER_EN)

    def test_keeps_exactly_one_when_the_model_wrote_two(self):
        out = apply_policy(f"{DOSE_EN} {DISCLAIMER_EN} {DISCLAIMER_EN}")
        assert out.count("consult a licensed pharmacist") == 1

    def test_the_line_matches_the_language_of_the_answer(self):
        assert apply_policy(DOSE_MM).endswith(DISCLAIMER_MM)
        assert apply_policy(DOSE_EN).endswith(DISCLAIMER_EN)

    def test_it_is_always_the_last_line(self):
        out = apply_policy(f"{DISCLAIMER_EN} {DOSE_EN}")
        assert out.endswith(DISCLAIMER_EN)
        assert out.count("consult a licensed pharmacist") == 1

    def test_force_false_strips_without_inspecting(self):
        """The fast path only answers stock, so it can skip detection."""

        assert apply_policy(f"{DOSE_EN} {DISCLAIMER_EN}", force=False) == DOSE_EN

    def test_force_true_attaches_without_inspecting(self):
        assert apply_policy(PRICE_EN, force=True).endswith(DISCLAIMER_EN)

    def test_empty_input_is_returned_unchanged(self):
        assert apply_policy("") == ""
        assert apply_policy(None) is None

    def test_an_answer_that_is_only_a_disclaimer_survives(self):
        """Stripping everything must not produce an empty bubble."""

        assert apply_policy(DISCLAIMER_EN).strip() != ""


class TestTheMeasuredInconsistency:
    """The three live answers that motivated this module.

    Same question class, three runs, two different behaviours. After this, all
    three land the same way.
    """

    def test_all_three_price_answers_now_agree(self):
        variants = [
            f"**ROYAL-D 25G** 1000000015837 — 800 MMK. {DISCLAIMER_EN}",
            "**ROYAL-D 25G** 1000000015837 — 800 MMK ဖြစ်ပါသည်။",
            "**ROYAL-D 25G** 1000000015837 is 800 MMK.",
        ]
        assert not any(has_disclaimer(apply_policy(v)) for v in variants)


class TestStreamFinisher:
    """The stream can only suppress a line it has not already sent.

    LeakFilter holds a paragraph until it is complete, so a safety line written
    as its own final paragraph is still in the buffer at flush time and can be
    dropped before anyone sees it. One written inline has already gone out.
    """

    def test_trailing_disclaimer_paragraph_is_never_streamed(self):
        from app.api import _finish_stream

        streamed = "**ROYAL-D 25G** 1000000015837 — 800 MMK."
        held = f"\n\n{DISCLAIMER_EN}"

        emit, final = _finish_stream(streamed, held, force=False)

        assert emit == "", "the line was still in the buffer; it must not go out"
        assert not has_disclaimer(final)

    def test_a_clinical_answer_gets_the_line_appended(self):
        from app.api import _finish_stream

        emit, final = _finish_stream(DOSE_EN, "")

        assert DISCLAIMER_EN in emit
        assert final.endswith(DISCLAIMER_EN)

    def test_inline_disclaimer_is_corrected_for_the_cache(self):
        """Already on screen — but must not be cached or replayed wrong."""

        from app.api import _finish_stream

        streamed = f"**ROYAL-D 25G** 1000000015837 — 800 MMK. {DISCLAIMER_EN}"
        emit, final = _finish_stream(streamed, "", force=False)

        assert not has_disclaimer(final)
        assert emit == ""

    def test_clean_stock_answer_passes_through_untouched(self):
        from app.api import _finish_stream

        emit, final = _finish_stream(STOCK_EN, "", force=False)
        assert emit == "" and final == STOCK_EN
