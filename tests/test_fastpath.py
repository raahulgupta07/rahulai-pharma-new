"""Fast-path tests — intent detection, deterministic resolution, scope, NULL stock.

All hermetic: the model layer is never called and the database layer is stubbed
(mirroring the no-network stance of ``tests/test_agent_concurrency.py``), so the
suite proves the fast path's decision logic without spending OpenRouter credit
or needing live Postgres.
"""

from __future__ import annotations

import asyncio

import pytest

from app import fastpath
from app import resolver
from app import tools
from app.resolver import Candidate, ResolveResult, Resolution


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message, intent, mention",
    [
        ("do I have paracap", fastpath.HOT_HAVE, "paracap"),
        ("is amoxil in stock", fastpath.HOT_HAVE, "amoxil"),
        ("do we stock flemex?", fastpath.HOT_HAVE, "flemex"),
        ("who else has paracap", fastpath.HOT_WHERE, "paracap"),
        ("where can I find flemex", fastpath.HOT_WHERE, "flemex"),
        ("which branch has paracap", fastpath.HOT_WHERE, "paracap"),
        # adjective between "which" and the store noun must still route to WHERE,
        # not fall through to the "... in stock" HAVE pattern (was a live bug:
        # "which other stores have X in stock" answered from the own store).
        ("which other stores have paracap in stock", fastpath.HOT_WHERE, "paracap"),
        ("which nearby branch has flemex", fastpath.HOT_WHERE, "flemex"),
        # Burmese (drug typed in English, question in Burmese — the real hot case)
        ("PARACAP ရှိလား", fastpath.HOT_HAVE, "PARACAP"),
        ("flemex ရှိသလား", fastpath.HOT_HAVE, "flemex"),
        ("ဘယ်ဆိုင်မှာ PARACAP ရှိလဲ", fastpath.HOT_WHERE, "PARACAP"),
    ],
)
def test_intent_positive(message, intent, mention):
    got = fastpath.detect_intent(message)
    assert got is not None, f"expected a hot intent for {message!r}"
    assert got[0] == intent
    assert got[1].lower() == mention.lower()


@pytest.mark.parametrize(
    "message",
    [
        "what is paracetamol used for?",
        "tell me about diabetes",
        "hello",
        "substitutes for paracap",
        "price of flemex",
        "do I have to take it with food",   # 'have to' must not read as a stock query
        "မင်္ဂလာပါ",                          # Burmese greeting
        "paracetamol ဆိုတာ ဘာလဲ",           # Burmese "what is paracetamol"
    ],
)
def test_intent_negative_falls_through(message):
    assert fastpath.detect_intent(message) is None


# ---------------------------------------------------------------------------
# Resolver (DB stubbed by layer)
# ---------------------------------------------------------------------------


def _make_fake_q(exact=None, alias=None, trigram=None):
    """Return a fake ``q`` that answers each resolver layer's query in turn."""

    async def fake_q(sql, *args):
        s = " ".join(sql.lower().split())
        if "drug_alias" in s:
            return list(alias or [])
        if "similarity(" in s:
            return list(trigram or [])
        if "from catalog" in s and "article_code = $1" in s:
            return list(exact or [])
        return []

    return fake_q


def test_resolver_exact_code(monkeypatch):
    monkeypatch.setattr(
        resolver, "q",
        _make_fake_q(exact=[{"article_code": "1000000024029",
                             "brand_name": "PARACAP", "generic_name": "Paracetamol"}]),
    )
    res = asyncio.run(resolver.resolve("1000000024029"))
    assert res.status is Resolution.RESOLVED
    assert res.source == "code"
    assert res.article_code == "1000000024029"
    assert res.confidence == 1.0


def test_resolver_alias_hit(monkeypatch):
    monkeypatch.setattr(
        resolver, "q",
        _make_fake_q(alias=[{"article_code": "1000000024029",
                             "brand_name": "PARACAP", "generic_name": "Paracetamol"}]),
    )
    res = asyncio.run(resolver.resolve("panadol"))
    assert res.status is Resolution.RESOLVED
    assert res.source == "alias"
    assert res.article_code == "1000000024029"


def test_resolver_trigram_hit(monkeypatch):
    monkeypatch.setattr(
        resolver, "q",
        _make_fake_q(trigram=[
            {"article_code": "A", "brand_name": "FLEMEX 10`S", "generic_name": None, "score": 0.583},
            {"article_code": "B", "brand_name": "FLEMEX SYRUP", "generic_name": None, "score": 0.389},
        ]),
    )
    res = asyncio.run(resolver.resolve("flemex"))
    assert res.status is Resolution.RESOLVED
    assert res.source == "trigram"
    assert res.article_code == "A"


def test_resolver_ambiguous(monkeypatch):
    monkeypatch.setattr(
        resolver, "q",
        _make_fake_q(trigram=[
            {"article_code": "A", "brand_name": "SARA PARACETAMOL", "generic_name": "Paracetamol", "score": 0.80},
            {"article_code": "B", "brand_name": "PARASAFE PARACETAMOL", "generic_name": "Paracetamol", "score": 0.78},
        ]),
    )
    res = asyncio.run(resolver.resolve("paracetmol"))
    assert res.status is Resolution.AMBIGUOUS
    assert res.article_code is None
    assert len(res.candidates) == 2


def test_resolver_not_found_garbage(monkeypatch):
    monkeypatch.setattr(resolver, "q", _make_fake_q())   # every layer empty
    res = asyncio.run(resolver.resolve("zxqwlkj"))
    assert res.status is Resolution.NOT_FOUND
    assert res.article_code is None


def test_resolver_not_found_below_floor(monkeypatch):
    monkeypatch.setattr(
        resolver, "q",
        _make_fake_q(trigram=[{"article_code": "A", "brand_name": "X",
                               "generic_name": None, "score": 0.10}]),
    )
    res = asyncio.run(resolver.resolve("whatever"))
    assert res.status is Resolution.NOT_FOUND


# ---------------------------------------------------------------------------
# Store scope + NULL stock (answer(), resolver + tools.q stubbed)
# ---------------------------------------------------------------------------


def _stub_resolved(monkeypatch, code="1000000015818", brand="FLEMEX 10`S"):
    async def fake_resolve(mention, **kw):
        return ResolveResult(
            Resolution.RESOLVED,
            [Candidate(code, brand, "Carbocisteine", 0.9)],
            source="trigram",
        )

    monkeypatch.setattr(fastpath, "resolve", fake_resolve)


def test_answer_enforces_store_scope(monkeypatch):
    """A scoped answer must query only the scoped store — never another site."""

    _stub_resolved(monkeypatch)
    captured = []

    async def fake_q(sql, *args):
        captured.append(args)
        return []

    monkeypatch.setattr(tools, "q", fake_q)

    scoped = "20005-CCYK"
    facts = asyncio.run(fastpath.answer("do i have flemex", scoped))
    assert facts is not None
    all_args = [a for args in captured for a in args]
    assert scoped in all_args, "scoped store was not passed into the query"
    assert "20099-CCZZ" not in all_args, "a foreign site leaked into the query"


def test_answer_null_stock_is_unknown_not_zero(monkeypatch):
    """A NULL stock_qty is preserved as None and rendered as unknown, never 0."""

    _stub_resolved(monkeypatch)

    async def fake_q(sql, *args):
        return [{"site_code": "20005-CCYK", "site_name": "Main", "stock_qty": None}]

    monkeypatch.setattr(tools, "q", fake_q)

    facts = asyncio.run(fastpath.answer("do i have flemex", "20005-CCYK"))
    assert facts["rows"][0]["stock_qty"] is None

    prompt = fastpath.build_phrasing_input("[Reply ONLY in English.]\n\ndo i have flemex", facts)
    assert '"stock_qty": null' in prompt
    # the phrasing contract forbids rendering unknown as zero
    assert "never" in fastpath.PHRASING_SYSTEM_PROMPT.lower()
    assert "unknown" in fastpath.PHRASING_SYSTEM_PROMPT.lower()


def test_answer_falls_through_when_not_found(monkeypatch):
    async def fake_resolve(mention, **kw):
        return ResolveResult(Resolution.NOT_FOUND)

    monkeypatch.setattr(fastpath, "resolve", fake_resolve)
    assert asyncio.run(fastpath.answer("do i have flemex", None)) is None


def test_answer_returns_none_for_non_hot_message():
    assert asyncio.run(fastpath.answer("tell me about diabetes", None)) is None
