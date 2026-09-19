"""Run the browser row builders' own checks under node, so the project keeps a single gate."""
import shutil
import subprocess
from pathlib import Path

import pytest

SUITE = Path(__file__).resolve().parents[1] / "web" / "diffrows.test.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the browser checks")
def test_browser_row_builders():
    result = subprocess.run(["node", "--test", str(SUITE)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
