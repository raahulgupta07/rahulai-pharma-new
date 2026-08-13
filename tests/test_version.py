"""Versioning: one source of truth, and notes that describe the build shipping.

The product had no version at all. That is survivable while one person
redeploys a laptop and stops being survivable the moment a pharmacist reports
"it did X yesterday" — with no version there is no way to ask which build they
were on, and CMHL runs a host we cannot reach whose contents we can only infer.

The failure mode worth engineering against is not "the version is absent" — you
notice that immediately. It is a version that is present and WRONG: a release
cut without updating the notes, so the console confidently describes the
previous release. These tests make that a red build.

No network, no LLM, no database — pure parsing and configuration, so they
cannot flake.
"""

from __future__ import annotations

import re

import pytest

from app import release_notes
from app.version import VERSION, version_info

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_version_is_semver():
    assert SEMVER.match(VERSION), f"{VERSION!r} is not MAJOR.MINOR.PATCH"


def test_changelog_parses_and_is_not_empty():
    """A changelog that silently parses to nothing renders as a blank page."""

    entries = release_notes.load()

    assert entries, "CHANGELOG.md parsed to zero releases"
    assert all(e["version"] for e in entries)
    assert all(e["sections"] for e in entries), "a release with no sections"


def test_the_top_release_note_describes_the_running_version():
    """The guard that matters.

    A release cut without touching CHANGELOG.md leaves the console describing
    the PREVIOUS release's changes under the new number — worse than no notes,
    because it reads as authoritative. Bumping `VERSION` now requires adding
    the matching entry, or this fails.
    """

    top = release_notes.latest()

    assert top is not None
    assert top["version"] == VERSION, (
        f"app/version.py says {VERSION} but the newest CHANGELOG.md entry is "
        f"{top['version']}. Add the release note, or fix the version."
    )


def test_release_versions_are_unique_and_ordered_newest_first():
    """Two entries for one version means somebody edited the wrong block."""

    versions = [e["version"] for e in release_notes.load()]

    assert len(versions) == len(set(versions)), f"duplicate versions: {versions}"

    def key(v):
        return tuple(int(x) for x in v.split(".")) if SEMVER.match(v) else (0, 0, 0)

    assert versions == sorted(versions, key=key, reverse=True), (
        f"releases are not newest-first: {versions}"
    )


def test_dev_build_does_not_claim_to_be_a_release():
    """An unstamped build must say so rather than inventing provenance.

    A wrong build stamp is worse than an absent one — it sends someone
    debugging a live incident to the wrong commit.
    """

    info = version_info()

    if info["git_sha"] == "dev" or not info["built_at"]:
        assert info["is_release_build"] is False


def test_version_info_shape_is_stable():
    """The admin UI and the public /version endpoint both read these keys."""

    info = version_info()

    assert set(info) == {
        "version", "git_sha", "git_sha_short", "built_at", "is_release_build",
    }
    assert info["version"] == VERSION


# ---- the parser itself -----------------------------------------------------


def test_parser_reads_versions_dates_and_bullets():
    parsed = release_notes.parse(
        "# Release notes\n\n"
        "## 2.1.0 — 2026-09-01\n\n"
        "### Added\n\n- A thing\n- Another thing\n\n"
        "### Fixed\n\n- A bug\n\n"
        "## 2.0.0 — 2026-08-01\n\n### Changed\n\n- Something else\n"
    )

    assert [r["version"] for r in parsed] == ["2.1.0", "2.0.0"]
    assert parsed[0]["date"] == "2026-09-01"
    assert parsed[0]["sections"]["Added"] == ["A thing", "Another thing"]
    assert parsed[0]["sections"]["Fixed"] == ["A bug"]


def test_parser_joins_a_wrapped_bullet():
    """Notes are hand-wrapped at ~80 cols; a wrapped line is one bullet.

    Without this every wrapped sentence renders as two half-sentences in the
    console, which is how a changelog stops being read.
    """

    parsed = release_notes.parse(
        "## 1.0.0 — 2026-01-01\n\n### Fixed\n\n"
        "- A long sentence that continues\n  onto a second line\n- Short one\n"
    )

    assert parsed[0]["sections"]["Fixed"] == [
        "A long sentence that continues onto a second line",
        "Short one",
    ]


def test_parser_ignores_preamble_and_prose():
    """Text above the first release, or between a heading and its sections."""

    parsed = release_notes.parse(
        "# Release notes\n\nSome preamble.\n\n"
        "## 1.0.0 — 2026-01-01\n\nA sentence about the release.\n\n"
        "### Fixed\n\n- The only bullet\n"
    )

    assert len(parsed) == 1
    assert parsed[0]["sections"] == {"Fixed": ["The only bullet"]}


def test_a_missing_changelog_is_empty_not_an_exception():
    """The API must not 500 because a file is absent in some deployment.

    The container copies CHANGELOG.md explicitly; if that COPY is ever dropped,
    the Version page should read "no notes", not take the endpoint down.
    """

    from pathlib import Path

    assert release_notes.load(Path("/nonexistent/CHANGELOG.md")) == []
    assert release_notes.latest(Path("/nonexistent/CHANGELOG.md")) is None


@pytest.mark.parametrize("junk", ["", "no headings at all", "## \n### Fixed\n- x\n"])
def test_parser_never_raises_on_malformed_input(junk):
    """A typo in a changelog must never be able to break the API."""

    assert isinstance(release_notes.parse(junk), list)
