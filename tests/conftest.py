from __future__ import annotations

from pathlib import Path
from typing import Callable

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.provider import MiniMaxClient


@pytest.fixture
async def make_client(tmp_path: Path) -> Callable:
    clients: list[httpx.AsyncClient] = []

    def factory(
        handler: Callable[[httpx.Request], httpx.Response],
        client_address: tuple[str, int] = ("127.0.0.1", 123),
    ) -> httpx.AsyncClient:
        settings = Settings(
            project_root=tmp_path,
            data_dir=tmp_path / "data",
            api_key="test-secret-sentinel",
            api_base_url="https://api.minimaxi.test",
        )
        provider = MiniMaxClient(settings, transport=httpx.MockTransport(handler))
        app = create_app(settings, provider)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=client_address),
            base_url="http://127.0.0.1:8000",
        )
        clients.append(client)
        return client

    yield factory

    for client in clients:
        await client.aclose()


def generation_request(client_request_id: str = "request-0001") -> dict:
    return {
        "client_request_id": client_request_id,
        "model": "MiniMax-H3",
        "content": [{"type": "text", "text": "A paper kite rises over a quiet salt flat."}],
        "resolution": "768P",
        "duration": 4,
        "ratio": "16:9",
        "aigc_watermark": False,
        "confirmed": False,
    }
