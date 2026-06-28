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


def test_session_state_is_exactly_the_contract_set():  # AC-13
    doc_fields = {"mr", "files", "highlights", "cards", "access_requests", "threads",
                  "messages", "drafts"}
    envelope = {"id", "status", "created_at", "seq"}
    assert set(SessionState.model_fields) == doc_fields | envelope
