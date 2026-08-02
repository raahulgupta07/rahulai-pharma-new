"""The reasoning leak, pinned with the text that actually reached pharmacists.

Every LEAK case below is transcribed from a screenshot in the 2026-07-28
feedback pack. Every CLEAN case is an answer that must survive untouched —
including the unhelpful "I could not find it" answers, which are a prompt
problem (Phase 3), not a leak, and must not be eaten by this filter.
"""

from app.answer_filter import LeakFilter, filter_answer, looks_like_reasoning

# ---- real leaked text (Feedback 5, 7, 10, 11, 12, 14) ----------------------

FB11 = (
    "thought We are in store 20015-CCGV. The search tools (search_by_meaning, "
    "search_by_name) are returning nothing or we are getting empty results, and "
    "list_sites only returns 20015-CCGV."
)
FB07 = (
    "Step 1: Check search_by_name with a very broad term just to see how names "
    "are formatted. Oh, actually search_by_name returns nothing."
)
FB10 = (
    "those table names look artificial (numbers only) — let me double check what "
    "kinds of articles are in the database. Wait! Let me query search_by_name "
    "with a very general expression or use a different search."
)
FB05_PROMPT_ECHO = (
    'Let me double-check the prompt instructions: "Reply ONLY in Burmese." '
    '"Keep numbers as Arabic digits (0-9)."'
)
FB12 = (
    "Wait, can we provide a safe, helpful general explanation of what Moriamin "
    "is, while explicitly stating it's not in the catalog?"
)

# ---- answers that must NOT be touched --------------------------------------

CLEAN_FOUND = (
    "AEROCORT INHALER (1000000008626) — 2 units at 20004-CCFMI, 36,500 MMK.\n\n"
    "Please consult a licensed pharmacist before use."
)
CLEAN_NOT_FOUND = (
    "I searched our catalog and inventory at branch 20004-CCFMI for \"Menthol\" "
    "but no matching products were found.\n\n"
    "Please consult a licensed pharmacist before use."
)
CLEAN_BURMESE = (
    "ဆိုင်ခွဲ 20061-CC50BTH တွင် BIOGESIC 500MG 10`S — ၁၄၂ ခု ရှိပါသည်။\n\n"
    "အသုံးမပြုမီ လိုင်စင်ရ ဆေးဝါးကျွမ်းကျင်သူနှင့် တိုင်ပင်ဆွေးနွေးပါ။"
)
CLEAN_LET_ME_KNOW = (
    "If this is known by a different brand or generic name, please let me know "
    "and I will gladly search again."
)


class TestClassifier:
    def test_real_leaks_are_caught(self):
        for text in (FB11, FB07, FB10, FB05_PROMPT_ECHO, FB12):
            assert looks_like_reasoning(text), f"missed a real leak: {text[:60]}"

    def test_real_answers_are_not_flagged(self):
        for text in (CLEAN_FOUND, CLEAN_NOT_FOUND, CLEAN_BURMESE, CLEAN_LET_ME_KNOW):
            assert not looks_like_reasoning(text), f"false positive: {text[:60]}"

    def test_a_tool_name_anywhere_is_decisive(self):
        assert looks_like_reasoning("The product is stocked. get_stock returned 4 rows.")

    def test_let_me_know_mid_sentence_is_ordinary_prose(self):
        """'let me' only counts as an opener, or every polite answer dies."""

        assert not looks_like_reasoning(CLEAN_LET_ME_KNOW)
        assert looks_like_reasoning("Let me search the catalog for that.")


class TestStreaming:
    def test_leading_reasoning_paragraph_is_suppressed(self):
        """Feedback 11's shape: reasoning first, real answer after."""

        f = LeakFilter()
        out = f.feed(FB11 + "\n\n")
        out += f.feed("BIOGESIC 500MG 10`S — 142 units at 20061-CC50BTH.")
        out += f.flush()

        assert "search_by_name" not in out
        assert "thought" not in out
        assert "BIOGESIC 500MG 10`S — 142 units" in out
        assert f.leaked is True

    def test_clean_answer_passes_through_byte_for_byte(self):
        f = LeakFilter()
        out = "".join(f.feed(c) for c in CLEAN_FOUND) + f.flush()

        assert out == CLEAN_FOUND
        assert f.leaked is False

    def test_delta_chunking_does_not_change_the_result(self):
        """Same text, different chunk boundaries, same output."""

        text = FB11 + "\n\n" + CLEAN_FOUND
        whole = filter_answer(text)

        f = LeakFilter()
        chunked = "".join(f.feed(text[i:i + 7]) for i in range(0, len(text), 7))
        chunked += f.flush()

        assert chunked.strip() == whole
        assert "search_by_meaning" not in chunked

    def test_mid_answer_leak_is_dropped_but_answer_survives(self):
        """Feedback 12: a real answer, then the model thinking out loud."""

        text = CLEAN_FOUND + "\n\n" + FB12
        out = filter_answer(text)

        assert "AEROCORT INHALER" in out
        assert "Wait, can we provide" not in out

    def test_an_entirely_leaked_answer_yields_a_safe_fallback(self):
        """Feedback 5 was reasoning end to end.

        Nothing survives filtering, but an empty bubble reads as a broken
        widget — so the customer gets an honest apology instead.
        """

        out = filter_answer(FB05_PROMPT_ECHO + "\n\n" + FB07)
        assert "search_by_name" not in out
        assert "prompt instructions" not in out.lower()
        assert "could not produce a reliable answer" in out

    def test_fallback_matches_the_language_of_the_answer(self):
        burmese_leak = "thought ဆိုင်ခွဲ 20015-CCGV ရှိ search_by_name ဘာမှ မပြန်ပါ။"
        out = filter_answer(burmese_leak)
        assert "search_by_name" not in out
        assert "လိုင်စင်ရ" in out  # Burmese fallback, not the English one

    def test_filter_answer_can_return_empty_when_asked(self):
        assert filter_answer(FB07, fallback=False) == ""


class TestDraftingScaffolding:
    """Observed live 2026-08-02, not in the original screenshots.

    Against a healthy catalog the model still leaked — but as a numbered
    self-review outline after the Burmese answer body, which none of the
    paragraph openers matched:

        ... တိုင်ပင်ဆွေးနွေးပါ။" (Using standard Myanmar phrasing).
        4.  **Final Polish
    """

    LIVE_LEAK = (
        'သုံးမပြုမီ လိုင်စင်ရ ဆေးဝါးပညာရှင်နှင့် တိုင်ပင်ဆွေးနွေးပါ။" '
        "(Using standard Myanmar phrasing).\n\n4.  **Final Polish"
    )

    def test_numbered_outline_item_is_a_leak(self):
        assert looks_like_reasoning("4.  **Final Polish")
        assert looks_like_reasoning("2) **Draft answer** — check the tone")

    def test_parenthetical_phrasing_note_is_a_leak(self):
        assert looks_like_reasoning("(Using standard Myanmar phrasing).")

    def test_the_live_leak_is_fully_suppressed(self):
        out = filter_answer(self.LIVE_LEAK)
        assert "Final Polish" not in out
        assert "Using standard Myanmar phrasing" not in out

    def test_ordinary_numbered_lists_survive(self):
        """Real answers do use numbered lists — those must not be eaten."""

        keep = "1. BIOGESIC 500MG 10`S — 380 units\n\n2. PARACAP 10`S — 663 units"
        out = filter_answer(keep)
        assert "BIOGESIC 500MG 10`S — 380 units" in out
        assert "PARACAP 10`S — 663 units" in out

    def test_numbered_BOLD_product_list_survives(self):
        """Regression: the multi-question answer shape.

        Found by the field-feedback eval on 2026-08-02. The first version of
        the drafting rule matched any numbered bold item, so a correct answer
        to Feedback Summary rule 8 — one numbered entry per question asked —
        was filtered down to "Based on the catalog:" plus the disclaimer. A
        filter that eats real answers is worse than the leak it prevents.
        """

        keep = (
            "Based on the catalog:\n\n"
            "1. **STROCAIN** 1000000008737 relieves stomach pain from gastritis.\n\n"
            "2. **MOTILIUM-M 10`S** 1000000008745 category is 5102-PRESCRIPTION MEDICINE.\n\n"
            "Please consult a licensed pharmacist before use."
        )
        out = filter_answer(keep)
        assert "STROCAIN" in out
        assert "5102-PRESCRIPTION MEDICINE" in out
        assert out == keep.strip()

    def test_mid_sentence_self_correction_is_caught(self):
        """Seen live 2026-08-02, mid-paragraph so the openers missed it."""

        assert looks_like_reasoning(
            "**BIOGESIC SUSPENSION 250MG (ORANGE)** -> wait, the brand name is ..."
        )
        assert looks_like_reasoning("It is 71 units. Wait, that is the other branch.")

    def test_wait_as_a_dosage_instruction_survives(self):
        """'Wait 30 minutes before eating' is real advice, not deliberation."""

        keep = "Take one tablet, then wait 30 minutes before eating."
        assert not looks_like_reasoning(keep)
        assert filter_answer(keep) == keep

    def test_numbered_drafting_heading_is_still_caught(self):
        """The narrow rule must still catch what it was written for."""

        assert looks_like_reasoning("4.  **Final Polish")
        assert looks_like_reasoning("2. Review the tone before sending")
        assert looks_like_reasoning("3) **Translate** the disclaimer")

    def test_dosage_instructions_are_not_drafting(self):
        keep = "Dosage: 1-2 puffs, up to 4 times daily. Rinse mouth after use."
        assert not looks_like_reasoning(keep)


class TestArtifacts:
    def test_stext_prefix_is_stripped(self):
        """Feedback 14 opened with 'stextI searched the catalog...'."""

        out = filter_answer("stextI searched the catalog for AEROCORT INHALER.")
        assert out.startswith("I searched the catalog")

    def test_artifact_only_stripped_at_the_start(self):
        keep = "The dosage is 1-2 puffs. See the text on the pack."
        assert filter_answer(keep) == keep


class TestEmptyAndEdge:
    def test_empty_input(self):
        f = LeakFilter()
        assert f.feed("") == ""
        assert f.feed(None) == ""
        assert f.flush() == ""
        assert f.leaked is False

    def test_burmese_answer_is_untouched(self):
        assert filter_answer(CLEAN_BURMESE) == CLEAN_BURMESE.strip()

    def test_dropped_text_is_recorded_for_logging(self):
        f = LeakFilter()
        f.feed(FB11 + "\n\n")
        f.flush()
        assert f.dropped and "search_by_meaning" in f.dropped[0]
