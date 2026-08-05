from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_browser_pool_and_request_ledger() -> None:
    result = subprocess.run(
        ["node", "--test", "tests/browser_logic.test.mjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
