"""Paths and settings — the design's `~/.review-mate/` workspace boundary.

Settings are read from the environment here rather than passed down from the composition root, so
a deployment changes one variable and nothing has to thread a parameter through to reach it.

- `REVIEW_MATE_HOME` — the workspace directory (used by tests to point at a tmp dir).
- `REVIEW_MATE_BLOB_BUDGET_MB` — how much unfolded file text one process keeps.
"""
from __future__ import annotations

import os
from pathlib import Path


def review_mate_home() -> Path:
    env = os.environ.get("REVIEW_MATE_HOME")
    return Path(env) if env else Path.home() / ".review-mate"


def sessions_dir() -> Path:
    return review_mate_home() / "sessions"


#: how much whole-file text a process keeps for unfolding, when nothing says otherwise. Several
#: repositories' worth: the median tracked file in a real repository is a few kB, so this holds
#: thousands of them, while still being a bound on a server meant to run for weeks.
DEFAULT_BLOB_BUDGET_MB = 16


def blob_budget_bytes() -> int:
    """The unfolded-content budget in bytes.

    A value that is not a positive number is ignored rather than fatal: a typo in a deployment's
    environment should not stop a review server starting, and the default it falls back to is a
    working one.
    """
    raw = os.environ.get("REVIEW_MATE_BLOB_BUDGET_MB")
    try:
        megabytes = float(raw) if raw else DEFAULT_BLOB_BUDGET_MB
    except ValueError:
        megabytes = DEFAULT_BLOB_BUDGET_MB
    if megabytes <= 0:
        megabytes = DEFAULT_BLOB_BUDGET_MB
    return int(megabytes * 1024 * 1024)
