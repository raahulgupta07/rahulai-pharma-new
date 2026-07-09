"""Guard the countable claims the README makes about the code.

The README's tool count drifted from 10 to 12 unnoticed because nothing tied the
prose to `agent.TOOLS`. Numbers in documentation do not recompute themselves, so
the ones that a machine can check are checked here.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.agent import TOOLS

README = Path(__file__).resolve().parent.parent / "README.md"


def test_readme_tool_count_matches_agent():
    text = README.read_text(encoding="utf-8")
    claims = [int(n) for n in re.findall(r"agent \((\d+) tools\)", text)]

    assert claims, "README no longer states a tool count; update this guard or restore it"
    for claimed in claims:
        assert claimed == len(TOOLS), (
            f"README claims {claimed} tools, app.agent.TOOLS has {len(TOOLS)}. "
            f"Update the README diagram."
        )


def test_readme_names_no_nonexistent_tools():
    """Every backticked tool name in the README must exist in TOOLS."""
    text = README.read_text(encoding="utf-8")
    real = {t.__name__ for t in TOOLS}

    # Only check names the README presents as tools, i.e. `foo_bar()`.
    mentioned = set(re.findall(r"`([a-z_]+)\(\)`", text))
    # _site_clause is a helper, not a tool; it is discussed by name on purpose.
    mentioned -= {"_site_clause", "is_valid_credential", "get_settings"}

    unknown = mentioned - real
    assert not unknown, f"README references tools that do not exist: {sorted(unknown)}"
