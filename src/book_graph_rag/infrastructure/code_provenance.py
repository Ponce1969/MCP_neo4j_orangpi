"""Resolve the code provenance of a run: which commit produced a gate report.

A report whose ``code_commit`` is empty cannot be tied to the code that produced it, and an
inherited one is worse: the generation layer used to fall back to the *baseline's* commit, which
names code that did not run. The composition root asks git once per process, so the report carries
the checkout that produced it.

Outside a repository (a wheel install, a container without ``.git``) the answer is the explicit
``unknown`` rather than an empty field nobody can tell apart from "not collected", and a git that
fails, hangs or is missing never breaks a run: provenance is evidence, not a precondition.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: What a run reports when the checkout cannot be read. Explicit on purpose: an empty string reads
#: like a field nobody filled, and this one says the question was asked and could not be answered.
UNKNOWN_COMMIT = "unknown"


def current_code_commit(*, cwd: Path | None = None) -> str:
    """Return the short commit of the checkout, or ``UNKNOWN_COMMIT`` if it cannot be read."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except Exception:  # noqa: BLE001 - provenance must never break a run
        return UNKNOWN_COMMIT
    commit = result.stdout.strip()
    return commit or UNKNOWN_COMMIT
