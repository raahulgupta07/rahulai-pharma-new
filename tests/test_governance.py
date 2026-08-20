"""The Governance page: a proposal drawn as a proposal, and checked where it can be.

The redesign describes a process — five steps with an owner and an SLA each, a
decision log, four support tiers with response times. Nothing in this repository
or this deployment establishes any of it. No document names an AI working group,
an exec sponsor, an ops owner or an on-call rota; nothing enforces a response
time.

There are two ways to get this page wrong and the second is much worse than the
first. Leaving it empty hides a proposal somebody thought about, and the next
person invents a different one. Printing it as though it were operating tells a
branch pharmacist that Platform is on call and that a data gap is picked up
within one business day — and nobody is on call. That is not a presentation
problem.

So: the proposal is drawn in full and marked as unagreed, and every claim the
console can check is checked against the running system. This file makes sure it
stays that way, and that the checks are measurements rather than assertions.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "admin" / "src" / "routes" / "governance" / "+page.svelte"
ADMIN = ROOT / "app" / "admin.py"
AGENT = ROOT / "app" / "agent.py"


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text()


@pytest.fixture(scope="module")
def handler() -> str:
    """The governance block, and nothing else.

    Slicing to the next ROUTE rather than to the next section marker is how
    this file first read the whole architecture board as its own and reported
    six states it never emits. The marker below it is the boundary."""
    src = ADMIN.read_text()
    i = src.index("# ---- governance: the proposal")
    j = src.index("# ---- architecture: what this is made of")
    assert j > i, "the governance block has moved below the architecture block"
    return src[i:j]


# ---- the proposal must never read as established ----------------------------


def test_the_page_says_the_process_is_not_agreed(page):
    assert "proposed, not agreed" in page, (
        "the banner declaring this process unagreed is gone. Without it every "
        "owner and every response time below reads as something somebody signed"
    )
    assert "from nowhere else" in page or "and from nowhere" in page, (
        "the page no longer says where the owners and SLAs came from"
    )


def test_every_support_tier_that_is_not_confirmed_says_so(page):
    """Three of the four tiers are an intention. Only the fourth — that nobody
    is on call — is something this system can confirm, and it confirms it by
    having nothing that could page anybody."""
    block = re.search(r"const TIERS = \[(.*?)\n  \];", page, re.S)
    assert block, "the support tiers are gone"
    rows = re.findall(r"\{ tier: '([^']+)'.*?\}", block.group(1))
    assert len(rows) >= 4, f"only {len(rows)} tier(s) drawn"
    confirmed = re.findall(r"confirmed: '(\w+)'", block.group(1))
    assert confirmed == ["alerting"], (
        f"tiers claim to be confirmed by {confirmed}. The only tier this system "
        f"can establish is the one about nobody being on call"
    )
    assert "Proposed only" in page, (
        "an unconfirmed tier no longer says it is only a proposal, so a response "
        "time nobody agreed reads as one somebody owes"
    )


def test_nobody_on_call_is_measured_and_not_written(page, handler):
    assert '"id": "alerting"' in handler, "the alerting check is gone from the API"
    m = re.search(r'"id": "alerting".*?"state": "(\w+)"', handler, re.S)
    assert m and m.group(1) == "absent", (
        f"the alerting check reports {m.group(1) if m else 'nothing'}. If this "
        f"ever says in_place, something must actually page somebody"
    )
    assert "checkOf('alerting')" in page, (
        "the page no longer reads the alerting check, so its strongest claim "
        "would become a sentence somebody typed"
    )


def test_the_operate_step_contradiction_is_shown(page):
    """Step 5 of the proposal says "Runbook, alerts, monthly review". The fourth
    tier says nobody is on call. Both are on this page; only one is true."""
    assert "these two cannot both be true" in page, (
        "the page no longer points out that the Operate step promises alerts "
        "while the support tiers say nobody is paged"
    )


def test_no_owner_is_a_person(page):
    """A role or a document can be checked. An invented name cannot, and it is
    the one thing on this page a reader would take entirely on trust."""
    owners = re.findall(r"who: '([^']+)'", page)
    assert owners, "no owners found — the extraction is wrong, not the page"
    for who in owners:
        # "Firstname Lastname" with both capitalised, and not a known role.
        bad = re.search(r"\b([A-Z][a-z]{2,})\s+([A-Z][a-z]{2,})\b", who)
        allowed = {"Pharmacy", "Platform", "Ask", "Two", "Nobody", "Anyone", "Recorded", "Enforced", "Standing", "Set"}
        if bad and bad.group(1) not in allowed:
            pytest.fail(
                f"owner {who!r} looks like a person's name. Nothing in this "
                f"repository names an individual, and inventing one puts a real "
                f"person's name against a decision they never made"
            )


# ---- the decision log -------------------------------------------------------


def test_every_decision_says_who_and_says_when_it_cannot(page):
    block = re.search(r"const DECISIONS = \[(.*?)\n  \];", page, re.S)
    assert block, "the decision log is gone"
    entries = re.findall(r"\{\s*when: ([^,]+),\s*\n\s*what: '([^']*(?:\\'[^']*)*)'", block.group(1))
    assert len(entries) >= 8, f"only {len(entries)} decision(s) recorded"
    owners = re.findall(r"who: '([^']+)'", block.group(1))
    assert len(owners) == len(entries), (
        "a decision carries no owner. 'Nobody is recorded as owning this' is an "
        "answer; a blank is not"
    )


def test_the_open_decisions_name_who_they_wait_on(page):
    """Three product decisions have been with the customer since 2026-08-03.
    An open decision with no name against it is one nobody chases."""
    block = re.search(r"const DECISIONS = \[(.*?)\n  \];", page, re.S)
    opens = re.findall(r"who: '([^']+)',\s*\n\s*state: 'open'", block.group(1))
    assert len(opens) >= 3, f"only {len(opens)} open decision(s) name an owner"
    for who in opens:
        assert "CMHL" in who, f"open decision waits on {who!r} rather than the customer"
        assert "no answer recorded" in who, (
            "an open decision no longer says that no answer has come back"
        )


def test_a_decisions_check_id_is_one_the_api_emits(page, handler):
    """A decision pointing at a check that no longer exists would render as
    'could not be checked' forever — which reads as a shrug rather than a bug."""
    wanted = set(re.findall(r"check: '(\w+)'", page))
    emitted = set(re.findall(r'"id": "(\w+)"', handler))
    missing = sorted(wanted - emitted)
    assert not missing, f"the page asks for check(s) {missing} that the API never returns"


def test_a_check_that_did_not_come_back_is_not_a_pass(page):
    assert "not checkable from here" in page, (
        "a decision with no check no longer says so"
    )
    assert "Could not be checked" in page, (
        "a check that failed to load has no state of its own, so a missing "
        "answer would look like a passing one"
    )


# ---- the checks are measurements --------------------------------------------


def test_read_only_is_checked_against_the_tool_list(handler):
    """A prompt can be argued with. The tool list cannot."""
    assert "from app.agent import TOOLS" in handler, (
        "the read-only claim is no longer checked against the tools the agent "
        "is actually given"
    )
    # Read what each tool DOES. The first version of this check matched write
    # VERBS in the tool name, and a tool named `reorder_stock` writes while
    # starting with none of them — a blind spot the check did not disclose,
    # which is worse than no check because it gets quoted as one.
    assert "inspect.getsource" in handler, (
        "the read-only check is back to reading tool NAMES rather than what the "
        "tools execute"
    )
    assert re.search(r"insert\\s\+into|delete\\s\+from", handler), (
        "the check no longer looks for write SQL in the tools' own code"
    )
    assert '"unknown" if unread' in handler, (
        "a tool whose source could not be read counts as a pass, so a tool the "
        "check cannot see would be reported as read-only"
    )
    # And the claim itself, recomputed here the same way the endpoint does it.
    import inspect

    from app.agent import TOOLS

    write_sql = re.compile(
        r"\b(insert\s+into|update\s+\w+\s+set|delete\s+from|truncate|"
        r"alter\s+table|drop\s+table|create\s+table)\b",
        re.I,
    )
    writers, unread = [], []
    for t in TOOLS:
        name = getattr(t, "__name__", str(t))
        try:
            if write_sql.search(inspect.getsource(t)):
                writers.append(name)
        except (OSError, TypeError):
            unread.append(name)
    assert not writers, (
        f"the assistant has been given {writers}, which write. The read-only "
        f"decision on the governance page is no longer true"
    )
    assert not unread, f"could not read the source of {unread}, so this proves nothing"


def test_one_vendor_is_read_from_the_calls_that_were_made(handler):
    assert "FROM llm_calls" in handler, (
        "the one-vendor claim is no longer read from the call log, so a second "
        "vendor slipping in would not show up here"
    )


def test_accuracy_is_still_named_as_ungraded(handler):
    assert '"id": "accuracy_graded"' in handler, "the accuracy check is gone"
    m = re.search(r'"id": "accuracy_graded".*?"state": "(\w+)"', handler, re.S)
    assert m and m.group(1) == "absent", (
        "the page claims accuracy is graded. Nothing stores an eval result on "
        "this deployment"
    )


def test_every_state_the_api_emits_is_drawn(handler, page):
    emitted = set(re.findall(r'"state": "(\w+)"', handler))
    assert emitted <= {"in_place", "absent", "unknown"}, f"unexpected state(s): {emitted}"
    for st in emitted:
        assert re.search(rf"\b{st}: \{{", page), f"the page has no styling for state {st!r}"


def test_a_data_file_with_no_blanks_is_not_read_as_a_broken_rule(handler):
    """The blank-is-not-zero decision is about what the code DOES with a blank.
    A file that happens to contain none does not disprove it, and reporting that
    as a failure would be reading the data as the decision."""
    assert "reading the DATA as the DECISION" in handler, (
        "the note explaining why an all-populated file still passes this check "
        "is gone; without it somebody will 'fix' it into a false negative"
    )
