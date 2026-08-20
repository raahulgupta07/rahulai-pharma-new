"""Guard what the embed widget puts on a customer's page.

The widget renders Markdown from the model and structured rows from the tools
into innerHTML, on third-party sites we do not control. Two things must hold and
neither can be checked by reading:

1. This catalog's product names survive. 2,790 of 5,292 rows carry a backtick as
   an apostrophe (PARACAP PARACETAMOL 10`S), which is Markdown's inline-code
   delimiter — the failure is silent and mangles the name mid-answer.
2. Nothing in the model's answer, or in a product name, can become markup.

So the REAL app/static/widget.js is executed through node against a DOM stub
(tests/js/widget_harness.js) and driven with the SSE frames the backend really
sends. Every assertion below is on HTML the shipped widget actually produced.

The widget carries its own copy of the admin's renderMarkdown. That duplication
is deliberate (see test_widget_and_admin_renderers_agree) and this file is what
stops it drifting in silence.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "js" / "widget_harness.js"
WIDGET = ROOT / "app" / "static" / "widget.js"
ADMIN_MD = ROOT / "admin" / "src" / "lib" / "aurora" / "markdown.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not WIDGET.exists(),
    reason="node (or the widget source) unavailable",
)


def _node(args: list[str], job: dict) -> dict:
    out = subprocess.run(
        ["node", *args],
        input=json.dumps(job),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def answer_frames(markdown: str) -> list[str]:
    """The frames the backend emits for a plain streamed answer."""

    return ["data: " + json.dumps({"delta": markdown}), "data: [DONE]"]


def render(markdown: str) -> str:
    """Stream `markdown` to the widget and return the HTML it wrote."""

    return render_many([markdown])[0]


def render_many(sources: list[str]) -> list[str]:
    job = {"turns": [{"frames": answer_frames(s)} for s in sources]}
    return [t["md"] for t in _node([str(HARNESS)], job)["turns"]]


def turn(frames: list[str]) -> dict:
    t = _node([str(HARNESS)], {"turns": [{"frames": frames}]})["turns"][0]
    # The trace is transient by design — the widget clears it when the answer
    # lands. What the customer saw is the last non-empty state, not the last.
    t["trace"] = next((h for h in reversed(t["stepsHistory"]) if h), "")
    return t


# --------------------------------------------------------------------------
# 1. Markdown reaches the DOM at all (it used to arrive as literal characters)
# --------------------------------------------------------------------------


def test_markdown_is_rendered_not_shown_as_literal_characters():
    html = render(
        "**RELYTE ORAL REHYDRATION SALTS 20.5G** is in stock.\n\n"
        "| Product | Site | Qty |\n"
        "|---|---|---|\n"
        "| RELYTE | 20026-CC19 | 6533 |\n"
    )
    assert "<strong>RELYTE ORAL REHYDRATION SALTS 20.5G</strong>" in html
    assert "<table>" in html and "<th>Qty</th>" in html and "<td>6533</td>" in html
    # the literal syntax must be gone, not merely accompanied by markup
    assert "**" not in html
    assert "|---|" not in html


def test_headings_lists_and_links_render():
    html = render(
        "## Substitutes\n"
        "- PARAGEN\n"
        "- PARASAFE\n\n"
        "See [the label](https://example.com/x) for details.\n"
    )
    assert "<h3>Substitutes</h3>" in html
    assert "<ul><li>PARAGEN</li><li>PARASAFE</li></ul>" in html
    assert '<a href="https://example.com/x"' in html


# --------------------------------------------------------------------------
# 2. Real product names — the backtick-as-apostrophe collision
# --------------------------------------------------------------------------

# Verified present in the live :8091 catalog on 2026-08-13. The last one is the
# only name in all 2,790 whose two apostrophes enclose no space and no `*`.
REAL_NAMES = [
    "PARACAP PARACETAMOL 10`S",
    "MEDIGESIC PARACETAMOL 500MG 10`S",
    "SOMPRAZ -20 10`S",
    "WOODS` PEPPERMINT LOZENGES 6`S (CHERRY )",
    "BRAND`S BIRD`S NEST 2.5OZ",
    "(RJ)B&L SOFLENS PWR(F`V)-3.00 CONTACT LENS 6`S",
    "REAL SLIM SHAKE 20`S(VANILLA/S`BERRY/CHOCO)",
]


def test_real_product_names_survive_the_backtick_apostrophe():
    """No name may be turned into a <code> span or lose characters."""

    sources = [f"**{n}** (`1000000380965`) is in stock." for n in REAL_NAMES]
    for name, html in zip(REAL_NAMES, render_many(sources)):
        expected = (
            name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        assert f"<strong>{expected}</strong>" in html, (name, html)
        # exactly one code span, and it is the article code
        assert html.count("<code>") == 1, (name, html)
        assert "<code>1000000380965</code>" in html, (name, html)


def test_apostrophe_backtick_does_not_swallow_the_bold_marker():
    """The original failure: `10`S` pairing with the article code's backtick."""

    html = render("**MEDIGESIC PARACETAMOL 500MG 10`S** (`1000000380965`) - fever.")
    assert "<strong>MEDIGESIC PARACETAMOL 500MG 10`S</strong>" in html
    # the paren between the name and the code is not eaten
    assert "(<code>" in html


def test_genuine_code_spans_still_render():
    """The opener rule must not cost us the spans the model really writes."""

    for src, want in [
        ("Use `get_stock` for this", "<code>get_stock</code>"),
        ("Price of `1000000348226` is 4500 MMK", "<code>1000000348226</code>"),
        ("(`1000000380965`) - fever", "<code>1000000380965</code>"),
        ("code:`1000000008780`", "<code>1000000008780</code>"),
    ]:
        assert want in render(src), src


def test_two_codes_on_one_line_both_render():
    html = render("Both `1000000348226` and `1000000380965` are stocked.")
    assert html.count("<code>") == 2


# --------------------------------------------------------------------------
# 3. XSS — the widget renders model output into a customer's page
# --------------------------------------------------------------------------

XSS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<iframe src=javascript:alert(1)></iframe>",
    "**<svg/onload=alert(1)>**",
    "`<b>x</b>`",
    "## <script>alert(1)</script>",
    "- <script>alert(1)</script>",
    "```\n<script>alert(1)</script>\n```",
    "| <script>alert(1)</script> |\n|---|\n| <img src=x onerror=alert(1)> |",
    "[click](javascript:alert(1))",
    "[click](https://x.test/a\" onmouseover=\"alert(1))",
    'text" onmouseover="alert(1)',
]


def test_no_xss_vector_produces_live_markup():
    for src, html in zip(XSS, render_many(XSS)):
        low = html.lower()
        assert "<script" not in low, (src, html)
        assert "<img" not in low, (src, html)
        assert "<iframe" not in low, (src, html)
        assert "<svg" not in low, (src, html)
        # A tag that was escaped is inert; what must never appear is a live one.
        assert "&lt;" in low or "<" not in low.replace("<p>", "").replace("</p>", ""), (src, html)
        # no attribute may have been broken out of, and no live javascript: URL
        assert "onmouseover=\"alert" not in low, (src, html)
        assert "onerror=" not in low.split("&lt;")[0], (src, html)
        assert 'href="javascript:' not in low, (src, html)


def test_a_product_name_cannot_inject_through_the_data_table():
    """The `result` rows are tool output; they land in innerHTML too."""

    t = turn(
        [
            "event: result\ndata: "
            + json.dumps(
                {
                    "tool": "search_by_name",
                    "rows": [{"brand_name": "<img src=x onerror=alert(1)>", "stock_qty": None}],
                }
            ),
            "data: " + json.dumps({"delta": "One match."}),
            "data: [DONE]",
        ]
    )
    # Stronger than it used to be. This asserted the name was ESCAPED into the
    # data table; the table is gone, so hostile row content never reaches the
    # DOM at all. Row values from a partner's product file are now outside the
    # rendered surface entirely — the citation names sources, never row text.
    assert "<img" not in t["data"].lower()
    assert "onerror" not in t["data"].lower()
    assert "alert(1)" not in t["data"]


def test_a_step_argument_cannot_inject_through_the_trace():
    t = turn(
        [
            "event: step\ndata: "
            + json.dumps({"label": "search_by_name", "args": {"name": "<script>alert(1)</script>"}})
        ]
    )
    assert "<script" not in t["trace"].lower()
    assert "&lt;script&gt;" in t["trace"]


# --------------------------------------------------------------------------
# 4. The SSE trace reaches the DOM (step/result used to be parsed and dropped)
# --------------------------------------------------------------------------


def test_step_frames_render_as_trace_lines():
    t = turn(
        [
            "event: step\ndata: " + json.dumps({"label": "search_by_name", "args": {"name": "OMEZ"}}),
            "event: step\ndata: " + json.dumps({"label": "get_stock", "args": {"detail": "OMEZ"}}),
        ]
    )
    assert "Searching for" in t["trace"] and "OMEZ" in t["trace"]
    assert "Checking stock of OMEZ" in t["trace"]


def test_the_citation_names_the_source_not_the_rows():
    """A citation says where an answer came from, not what is in the database.

    This used to render every row the tools returned as an expandable table —
    105 rows behind "Show what I checked (105)". That is a data export: it
    answers "what is in your database", which nobody asked, instead of "where
    did this answer come from", which is the only thing a citation is for.
    """

    rows = [
        {"brand_name": "ROYAL-D 25G", "site_code": "20052-CCTLKK", "stock_qty": 4154},
        {"brand_name": "ROYAL-D 25G", "site_code": "20024-CC73", "stock_qty": 2298},
    ]
    t = turn(
        [
            "event: step\ndata: " + json.dumps({"label": "get_stock", "args": {"name": "ROYAL-D"}}),
            "event: result\ndata: " + json.dumps({"tool": "get_stock", "rows": rows}),
            "data: " + json.dumps({"delta": "Two branches have it."}),
            "data: [DONE]",
        ]
    )

    assert "stock levels" in t["data"]
    assert "2 branches" in t["data"]
    # the raw dump and its database vocabulary are gone
    assert "View data" not in t["data"]
    assert "Show what I checked" not in t["data"]
    assert "<table" not in t["data"]
    # the trace still showed the work while it ran
    assert "Checking stock of ROYAL-D" in t["trace"]
    assert "Two branches have it." in t["md"]


def test_the_citation_names_every_distinct_source_once():
    """Two catalogue tools are one source, not two lines of jargon."""

    t = turn(
        [
            "event: result\ndata: " + json.dumps({"tool": "search_by_name", "rows": [{"a": 1}]}),
            "event: result\ndata: " + json.dumps({"tool": "get_article_info", "rows": [{"a": 1}]}),
            "event: result\ndata: " + json.dumps({"tool": "get_stock", "rows": [{"b": 2}]}),
            "data: " + json.dumps({"delta": "ok"}),
            "data: [DONE]",
        ]
    )

    assert t["data"].count("product catalogue") == 1
    assert "stock levels" in t["data"]
    # never the internal tool names
    for tool in ("search_by_name", "get_article_info", "get_stock"):
        assert tool not in t["data"]


def test_a_turn_with_no_rows_has_no_citation():
    """Nothing was consulted, so claiming a source would be a lie."""

    t = turn(["data: " + json.dumps({"delta": "No results."}), "data: [DONE]"])
    assert "cca-cite" not in t["data"]
    assert "Checked against" not in t["data"]


def test_a_single_branch_is_not_pluralised():
    rows = [{"brand_name": "ROYAL-D 25G", "site_code": "20052-CCTLKK", "stock_qty": 4154}]
    t = turn(
        [
            "event: result\ndata: " + json.dumps({"tool": "get_stock", "rows": rows}),
            "data: " + json.dumps({"delta": "One branch."}),
            "data: [DONE]",
        ]
    )
    assert "1 branch" in t["data"] and "1 branches" not in t["data"]


# --------------------------------------------------------------------------
# 5. Drift — the widget's renderer is a copy, and copies rot
# --------------------------------------------------------------------------

# Sharing one source between the two renderers would mean either an import (the
# widget must stay a dependency-free classic script) or the server concatenating
# them at /api/embed/widget.js (app/api.py, which this change may not touch).
# So the copy stays, and this test makes the copy loud: both renderers run the
# same corpus and every difference must be listed here with a reason.
PARITY_CORPUS = [
    "**RELYTE ORAL REHYDRATION SALTS 20.5G** is in stock.",
    "**MEDIGESIC PARACETAMOL 500MG 10`S** (`1000000380965`) - fever.",
    "## Substitutes\n- PARAGEN\n- PARASAFE\n\n1. first\n2. second",
    "| Product | Qty |\n|---|---|\n| SOMPRAZ -20 10`S | 42 |",
    "Use `get_stock` here. *emphasis* and **bold**.",
    "```\ncode block\n```",
    "<script>alert(1)</script>",
    "Bare code 1000000348226 in a sentence.",
    "WOODS` PEPPERMINT LOZENGES 6`S (CHERRY )",
]

# Declared, intentional differences. Each entry normalises the ADMIN output
# towards the widget's; if one of these ever stops applying the test fails and
# whoever changed a renderer has to come back here and say why.
DECLARED_DIVERGENCES = """
- the admin chips 10-14 digit article codes into a <button data-code> for its
  source drawer; the widget has no drawer to open (that endpoint is unscoped —
  see CLAUDE.md) so it leaves the digits as text
- class attributes: the admin uses Tailwind-adjacent md-* classes, the widget
  uses its own scoped cca-* ones, because it cannot see the admin's CSS
- the widget wraps tables in <div class="cca-tw"> for horizontal scroll inside a
  376px panel
- the widget also escapes ' (&#39;); the admin does not
- the widget requires a literal http(s):// in a link URL and puts the URL in
  href. The ADMIN PUTS THE LINK TEXT IN href ($1, not $2) — a live bug in
  admin/src/lib/aurora/markdown.js, reported, not fixed here (out of scope)
- the widget's inline-code opener may not follow a letter or digit, so that
  REAL SLIM SHAKE 20`S(VANILLA/S`BERRY/CHOCO) is not eaten
"""


def _admin_render(sources: list[str]) -> list[str]:
    script = (
        f"import {{ renderMarkdown }} from {json.dumps(str(ADMIN_MD))};"
        "import fs from 'node:fs';"
        "const src = JSON.parse(fs.readFileSync(0,'utf8'));"
        "process.stdout.write(JSON.stringify(src.map(renderMarkdown)));"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=json.dumps(sources),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _normalise(html: str) -> str:
    """Erase the DECLARED differences and nothing else."""

    import re

    # An article code: a chip in the admin, a <code> span or bare text in the
    # widget. Reduce all three spellings to the bare digits.
    html = re.sub(r'<button type="button"[^>]*data-code="(\d{10,14})">\1</button>', r"\1", html)
    html = re.sub(r"<code[^>]*>(\d{10,14})</code>", r"\1", html)
    # scoped class names (md-* vs cca-*) and the widget's table scroll wrapper
    html = re.sub(r'\s*class="(md-|cca-)[^"]*"', "", html)
    html = html.replace("</table></div>", "</table>").replace("<div>", "")
    html = html.replace("&#39;", "'")
    # Anchor text only: the admin's href is the link TEXT, not the URL (bug,
    # see DECLARED_DIVERGENCES), so hrefs cannot be compared.
    html = re.sub(r'<a href="[^"]*"[^>]*>', "<a>", html)
    return html


@pytest.mark.skipif(not ADMIN_MD.exists(), reason="admin source unavailable")
def test_widget_and_admin_renderers_agree():
    __doc__ = DECLARED_DIVERGENCES  # noqa: F841  (kept next to the assertion)

    widget = [_normalise(h) for h in render_many(PARITY_CORPUS)]
    admin = [_normalise(h) for h in _admin_render(PARITY_CORPUS)]

    for src, w, a in zip(PARITY_CORPUS, widget, admin):
        assert w == a, (
            f"the widget's copy of renderMarkdown has drifted from the admin's.\n"
            f"source: {src!r}\nwidget: {w!r}\nadmin : {a!r}\n"
            f"If the difference is intentional, add it to DECLARED_DIVERGENCES "
            f"and to _normalise().{DECLARED_DIVERGENCES}"
        )


def test_the_admin_renderer_still_eats_the_one_bad_product_name():
    """Pins the divergence above, so it cannot be forgotten.

    The opener fix lives only in the widget — this change may not edit the admin
    source. When someone ports it across, this test fails and the divergence
    comes off the list.
    """

    bad = "REAL SLIM SHAKE 20`S(VANILLA/S`BERRY/CHOCO)"
    assert "<code" in _admin_render([bad])[0]
    assert "<code" not in render(bad)
