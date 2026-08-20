"""The product version — one source of truth, read by everything else.

Nothing in this repo had a version. That is fine while one person redeploys a
laptop, and stops being fine the moment a pharmacist says "it did X yesterday":
with no version there is no way to ask *which build* they were on, and CMHL
runs a host we cannot reach (192.168.2.46) whose contents we can only infer.
A visible version turns "is your copy current?" from a guess into a question
with an answer.

Three fields, deliberately separate:

* ``VERSION`` — the release, hand-edited when we cut one. Semantic-ish: bump
  MINOR for features, PATCH for fixes. This is what a human quotes.
* ``GIT_SHA`` / ``BUILT_AT`` — stamped at image build via build args. These are
  what an engineer needs, and they are the half that cannot be faked by
  forgetting to edit a file. A dev run reports "dev"/unknown rather than
  pretending, because a wrong build stamp is worse than an absent one.

The release notes live in ``CHANGELOG.md`` at the repo root and are parsed by
``app/release_notes.py``. Keep the top entry's version equal to ``VERSION`` —
``tests/test_version.py`` fails the build if they drift, which is the only
thing that reliably stops a release going out described as its predecessor.
"""

from __future__ import annotations

import os
from typing import Dict

VERSION = "1.5.0"

# Injected by docker/Dockerfile via --build-arg. Absent in a local dev run.
GIT_SHA = os.getenv("GIT_SHA", "dev")
BUILT_AT = os.getenv("BUILT_AT", "")


def version_info() -> Dict[str, str]:
    """The version payload served by /version and shown in the admin UI."""

    return {
        "version": VERSION,
        "git_sha": GIT_SHA,
        # Short sha is what people actually read and paste into a ticket.
        "git_sha_short": GIT_SHA[:7] if GIT_SHA and GIT_SHA != "dev" else GIT_SHA,
        "built_at": BUILT_AT,
        # True only for an image built by the Dockerfile. The admin UI badges a
        # dev build so nobody reports a bug against a build that never shipped.
        "is_release_build": GIT_SHA != "dev" and bool(BUILT_AT),
    }


__all__ = ["VERSION", "GIT_SHA", "BUILT_AT", "version_info"]
