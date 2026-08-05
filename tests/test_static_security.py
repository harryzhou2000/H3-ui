from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_browser_bundle_has_no_provider_key_or_direct_provider_calls() -> None:
    browser_source = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "app" / "static").glob("*")
        if path.is_file()
    )
    assert "MINIMAX_API_KEY" not in browser_source
    assert "Authorization" not in browser_source
    assert "api.minimaxi.com" not in browser_source
    assert "test-secret-sentinel" not in browser_source


def test_env_and_runtime_data_are_ignored() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in ignore
    assert ".venv/" in ignore
    assert "data/assets/*" in ignore
    assert "data/downloads/*" in ignore
