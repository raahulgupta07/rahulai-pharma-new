"""Guard the chat Markdown renderer against this catalog's real product names.

53% of catalog rows carry a backtick as an apostrophe ("PARACAP PARACETAMOL
10`S"). That character collides with Markdown's inline-code delimiter, and the
answers the model writes put a backticked article code on the same line as the
product name — so the two backticks pair with each other and eat everything
between them.

There is no JS test runner in this repo, so the real module is executed through
node. Skips (rather than fails) where node is unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MD = Path(__file__).resolve().parent.parent / "admin" / "src" / "lib" / "aurora" / "markdown.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not MD.exists(),
    reason="node (or the admin source) unavailable",
)


def render(src: str) -> str:
    """Run renderMarkdown(src) in node and return the HTML."""

    script = (
        f"import {{ renderMarkdown }} from {json.dumps(str(MD))};"
        f"process.stdout.write(renderMarkdown({json.dumps(src)}));"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return out.stdout


def test_backtick_apostrophe_does_not_swallow_bold_and_code():
    """`10`S` must not pair with the backtick opening the article code."""

    html = render("**MEDIGESIC PARACETAMOL 500MG 10`S** (`1000000380965`) - fever.")

    # The product name survives intact, apostrophe and all.
    assert "<strong>MEDIGESIC PARACETAMOL 500MG 10`S</strong>" in html
    # The code becomes a chip, not a mangled <code> span.
    assert 'data-code="1000000380965"' in html
    assert "<code" not in html
    # The paren between them is not swallowed.
    assert "(<button" in html


def test_article_code_chip_is_never_nested():
    """The bare-code pass must not re-wrap digits inside a chip it just emitted."""

    for src in ("Price of `1000000348226` is 4500 MMK", "Bare code 1000000348226 here"):
        html = render(src)
        assert html.count("<button") == 1, html
        assert "><button" not in html, html


def test_placeholder_sentinel_never_leaks():
    html = render("`1000000348226` and `get_stock` and 1000000380965")
    assert "\x00" not in html


def test_plain_inline_code_still_renders():
    assert '<code class="md-code">get_stock</code>' in render("Use `get_stock` for this")


def test_html_is_escaped_before_rendering():
    html = render("<script>alert(1)</script> and `x`")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# --------------------------------------------------------------------------
# Table column alignment.
#
# The separator row of a Markdown table carries the alignment (`---:` = right).
# It was parsed to find the table and then discarded, so every stock and price
# column the answer deliberately right-aligned rendered flush left — and the
# stylesheet's rule that gives number columns the mono face and tabular figures
# had nothing to match on.
# --------------------------------------------------------------------------

TABLE = (
    "| Branch | Stock | Price |\n"
    "| --- | ---: | ---: |\n"
    "| 10021-YKN | 148 | 1,200 |\n"
)


def test_right_aligned_columns_carry_the_alignment():
    html = render(TABLE)
    assert html.count('align="right"') == 4, html  # 2 headers + 2 cells


def test_left_aligned_column_carries_no_attribute():
    html = render(TABLE)
    assert '<th>Branch</th>' in html, html
    assert '<td>10021-YKN</td>' in html, html


def test_centre_alignment_is_carried_too():
    html = render("| A |\n| :-: |\n| x |\n")
    assert html.count('align="center"') == 2, html


def test_alignment_is_the_only_attribute_ever_emitted_on_a_cell():
    """A cell attribute is a hole in an otherwise escaped renderer. Keep it
    closed: alignment comes from a fixed set, never from cell content."""
    html = render("| a onclick=x | b |\n| ---: | --- |\n| <img src=y> | z |\n")
    assert "onclick" not in html or "onclick=x" in html.replace("&", "")
    assert "<img" not in html, html
