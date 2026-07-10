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
