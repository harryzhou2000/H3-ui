from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_pre_commit_guards_secrets_and_formats_python() -> None:
    config = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")

    assert "detect-secrets-hook" in config
    assert "detect-private-key" in config
    assert "ruff check --fix" in config
    assert "ruff format" in config
    assert "check_javascript.py" in config
    assert "repo: local" in config


def test_ci_runs_locked_offline_safe_quality_commands() -> None:
    workflow = (ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")

    assert "uv sync --locked" in workflow
    assert "uv run pre-commit run --all-files" in workflow
    assert "uv run pytest -q" in workflow
    assert "RUN_LIVE_MINIMAX_TESTS" not in workflow
