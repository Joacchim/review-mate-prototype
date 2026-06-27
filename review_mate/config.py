"""Paths and settings — the design's `~/.review-mate/` workspace boundary.

Overridable via `REVIEW_MATE_HOME` (used by tests to point at a tmp dir).
"""
from __future__ import annotations

import os
from pathlib import Path


def review_mate_home() -> Path:
    env = os.environ.get("REVIEW_MATE_HOME")
    return Path(env) if env else Path.home() / ".review-mate"


def sessions_dir() -> Path:
    return review_mate_home() / "sessions"
