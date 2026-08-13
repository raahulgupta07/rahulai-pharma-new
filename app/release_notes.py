"""Parse CHANGELOG.md into the structure the console renders.

The notes are authored as Markdown so they can be read in the repo, in a
browser, and pasted into an email to CMHL without a build step. This turns that
one file into JSON rather than keeping a second copy in the database, because
two copies of a release note drift and the wrong one is always the one someone
reads.

Deliberately forgiving in one direction only: an unrecognised heading is
dropped rather than raising, so a typo in a changelog can never take the API
down — but a missing or unparseable file returns an empty list, which the UI
shows as "no notes", not as a fake release. Silence is better than fiction here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

# "## 1.0.0 — 2026-08-13" (em dash or hyphen, date optional)
_RELEASE = re.compile(r"^##\s+(?P<version>\S+)\s*[—-]?\s*(?P<date>\d{4}-\d{2}-\d{2})?\s*$")
# "### Fixed"
_SECTION = re.compile(r"^###\s+(?P<name>.+?)\s*$")
# "- item" / "* item"
_BULLET = re.compile(r"^[-*]\s+(?P<text>.+?)\s*$")

# Rendered as coloured chips in the console; anything else is shown plainly.
KNOWN_SECTIONS = ("Added", "Fixed", "Changed", "Security", "Removed", "Deprecated")

CHANGELOG_PATH = Path(__file__).parent.parent / "CHANGELOG.md"


def _flush(bullet: List[str], parts: List[str]) -> None:
    """Close off a multi-line bullet, joining its continuation lines."""

    if parts:
        bullet.append(" ".join(p.strip() for p in parts).strip())
        parts.clear()


def parse(text: str) -> List[Dict]:
    """Parse changelog text into ``[{version, date, sections: {name: [str]}}]``."""

    releases: List[Dict] = []
    current: Optional[Dict] = None
    section: Optional[str] = None
    bullets: List[str] = []
    pending: List[str] = []

    for raw in text.splitlines():
        line = raw.rstrip()

        m = _RELEASE.match(line)
        if m:
            _flush(bullets, pending)
            current = {"version": m.group("version"), "date": m.group("date") or "", "sections": {}}
            releases.append(current)
            section, bullets = None, []
            continue

        if current is None:
            continue  # preamble above the first release

        m = _SECTION.match(line)
        if m:
            _flush(bullets, pending)
            section = m.group("name")
            bullets = current["sections"].setdefault(section, [])
            continue

        if section is None:
            continue  # prose between a version heading and its first section

        m = _BULLET.match(line)
        if m:
            _flush(bullets, pending)
            pending.append(m.group("text"))
            continue

        # A wrapped bullet continues the previous one; a blank line ends it.
        if line.strip() and pending:
            pending.append(line)
        else:
            _flush(bullets, pending)

    _flush(bullets, pending)
    return [r for r in releases if r["sections"]]


def load(path: Optional[Path] = None) -> List[Dict]:
    """Read and parse the changelog. Missing/unreadable file -> empty list."""

    try:
        return parse((path or CHANGELOG_PATH).read_text(encoding="utf-8"))
    except OSError:
        return []


def latest(path: Optional[Path] = None) -> Optional[Dict]:
    """The newest release entry, or None when there are no notes."""

    entries = load(path)
    return entries[0] if entries else None


__all__ = ["parse", "load", "latest", "KNOWN_SECTIONS", "CHANGELOG_PATH"]
