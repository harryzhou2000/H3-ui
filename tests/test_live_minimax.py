from __future__ import annotations

import os

import pytest

from app.config import Settings
from app.provider import MiniMaxClient


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_MINIMAX_TESTS") != "1",
    reason="Set RUN_LIVE_MINIMAX_TESTS=1 for the read-only authenticated probe",
)
@pytest.mark.asyncio
async def test_live_read_only_task_list_contract() -> None:
    settings = Settings.from_env()
    assert settings.api_key_configured, "MINIMAX_API_KEY is required"
    # The client validates only {items: list, total: int}; task data is discarded.
    await MiniMaxClient(settings).test_connection()
