from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.providers import (
    ProviderInfo,
    ProviderRegistry,
    VideoProvider,
)
from app.providers.factory import build_provider, default_provider_registry


class FakeVideoProvider:
    info = ProviderInfo("Local Fake", "fake-video-1", "Fake Contract")

    async def test_connection(self) -> None:
        return None

    async def create_video(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"task_id": "fake-task"}

    async def create_context_ir(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"task_id": "fake-ir"}

    async def regenerate_video(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"task_id": "fake-regen"}

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return {"task": {"task_id": task_id, "status": "queued"}}

    async def list_tasks(
        self,
        *,
        page_num: int = 1,
        page_size: int = 20,
        status: str | None = None,
        task_ids: list[str] | None = None,
        model: str | None = None,
        task_type: str | None = None,
    ) -> dict[str, Any]:
        return {"items": [], "total": 0}

    async def delete_task(self, task_id: str) -> dict[str, Any]:
        return {"task_id": task_id}

    async def upload_video_input(self, path: Path, filename: str, mime_type: str) -> dict[str, Any]:
        return {"file": {"file_id": "fake-file"}}

    async def list_video_inputs(self) -> dict[str, Any]:
        return {"files": []}

    async def download_result(self, url: str, destination: Path) -> int:
        destination.write_bytes(b"fake-video")
        return 10


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        api_key="",
        api_base_url="https://api.minimaxi.com",
    )


def test_default_registry_builds_the_minimax_adapter(tmp_path: Path) -> None:
    registry = default_provider_registry()
    provider = build_provider(settings_for(tmp_path), registry=registry)

    assert registry.names == ("minimax",)
    assert isinstance(provider, VideoProvider)
    assert provider.info == ProviderInfo("MiniMax", "MiniMax-H3", "Video Generation V2")


def test_registry_accepts_a_custom_provider_factory(tmp_path: Path) -> None:
    registry = ProviderRegistry()
    registry.register("local_ai", lambda settings: FakeVideoProvider())

    provider = build_provider(settings_for(tmp_path), name="local_ai", registry=registry)

    assert isinstance(provider, FakeVideoProvider)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("local_ai", lambda settings: FakeVideoProvider())


def test_registry_rejects_unknown_or_incompatible_providers(tmp_path: Path) -> None:
    registry = ProviderRegistry()
    with pytest.raises(ValueError, match="Unknown provider"):
        registry.create("missing", settings_for(tmp_path))

    registry.register("broken", lambda settings: object())  # type: ignore[arg-type,return-value]
    with pytest.raises(TypeError, match="incompatible"):
        registry.create("broken", settings_for(tmp_path))


async def test_app_health_uses_injected_provider_metadata(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path), FakeVideoProvider())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 123)),
        base_url="http://127.0.0.1:8000",
    ) as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "key_configured": False,
        "provider": "Local Fake",
        "model": "fake-video-1",
        "api_contract": "Fake Contract",
    }


async def test_fake_connection_probe_never_calls_a_network_api(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path), FakeVideoProvider())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 123)),
        base_url="http://127.0.0.1:8000",
    ) as client:
        response = await client.post("/api/connection/test")

    assert response.status_code == 200
    assert response.json()["authenticated"] is True


async def test_queued_task_pool_survives_a_server_restart(tmp_path: Path) -> None:
    from tests.conftest import generation_request

    settings = settings_for(tmp_path)
    first_app = create_app(settings, FakeVideoProvider())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=first_app, client=("127.0.0.1", 123)),
        base_url="http://127.0.0.1:8000",
    ) as client:
        created = await client.post(
            "/api/jobs",
            json={**generation_request("restart-safe-request"), "confirmed": True},
        )
        assert created.status_code == 201
        assert created.json()["job"]["status"] == "queued"

    assert settings.database_path.is_file()

    restarted_app = create_app(settings, FakeVideoProvider())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted_app, client=("127.0.0.1", 124)),
        base_url="http://127.0.0.1:8000",
    ) as client:
        jobs = (await client.get("/api/jobs")).json()

    assert [(job["task_id"], job["status"]) for job in jobs] == [("fake-task", "queued")]
