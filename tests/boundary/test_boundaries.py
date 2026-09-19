"""Boundary guards for the spine.

AC-12: the core carries no host/MCP/workspace/UI logic — those attach only through seams.
AC-13: SessionState exposes exactly the design's contract document set.
"""
import ast
from pathlib import Path

import pytest

from review_mate.session.state import SessionState

CORE = Path(__file__).resolve().parents[2] / "review_mate"
FORBIDDEN = {"server", "host", "gitlab", "glab", "mcp", "workspace_manager",
             "diff_browser", "browser_ui"}


def _import_names(py: Path) -> set[str]:
    tree = ast.parse(py.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


@pytest.mark.parametrize("py", sorted((CORE / "session").glob("*.py")) + [CORE / "seams.py"])
def test_core_imports_no_host_mcp_workspace_or_ui(py):  # AC-12
    for imp in _import_names(py):
        low = imp.lower()
        assert not any(tok in low for tok in FORBIDDEN), f"{py.name} imports forbidden {imp!r}"


# Layers that sit above the core: they fold and ship core state, and the core must not know they
# exist. Matched as module prefixes, not substrings — "review_mate" itself contains "view".
LAYERS_ABOVE_CORE = ("review_mate.view", "review_mate.server")


@pytest.mark.parametrize("py", sorted((CORE / "session").glob("*.py")) + [CORE / "seams.py"])
def test_core_does_not_import_the_layers_above_it(py):
    for imp in _import_names(py):
        offender = next((top for top in LAYERS_ABOVE_CORE
                         if imp == top or imp.startswith(top + ".")), None)
        assert offender is None, f"{py.name} imports {imp!r} — {offender} is above the core"


def test_session_state_is_exactly_the_contract_set():  # AC-13
    doc_fields = {"mr", "files", "highlights", "cards", "access_requests", "threads",
                  "messages", "drafts"}
    envelope = {"id", "status", "created_at", "seq"}
    workspace = {"checkout_path"}   # the on-disk MR checkout (code-graph / LSP / grep)
    assert set(SessionState.model_fields) == doc_fields | envelope | workspace
