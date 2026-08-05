from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.config import OFFICIAL_MINIMAX_API_BASE_URL, Settings

ROOT = Path(__file__).resolve().parent.parent


def test_importing_main_does_not_load_settings_or_create_the_runtime_app() -> None:
    script = "\n".join(
        [
            "from app.config import Settings",
            "def fail(*args, **kwargs):",
            "    raise RuntimeError('Settings.from_env called during import')",
            "Settings.from_env = classmethod(fail)",
            "import app.main",
            "assert not hasattr(app.main, 'app')",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_production_provider_host_is_pinned(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MINIMAX_API_BASE_URL", "https://collector.example")
    with pytest.raises(RuntimeError, match="must remain pinned"):
        Settings.from_env(tmp_path / "missing.env")


def test_official_provider_host_is_accepted(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MINIMAX_API_BASE_URL", OFFICIAL_MINIMAX_API_BASE_URL)
    settings = Settings.from_env(tmp_path / "missing.env")
    assert settings.api_base_url == OFFICIAL_MINIMAX_API_BASE_URL
