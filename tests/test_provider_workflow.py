from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx

import app.main as main_module
from app.main import _retry_after_seconds
from tests.conftest import generation_request


class StatefulMiniMax:
    def __init__(self) -> None:
        self.create_calls = 0
        self.tasks: dict[str, str] = {}
        self.saw_secret = False
        self.cdn_had_authorization = False
        self.query_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.example.test":
            self.cdn_had_authorization = "authorization" in request.headers
            return httpx.Response(
                200,
                content=b"fake-mp4-bytes",
                headers={"Content-Type": "video/mp4", "Content-Length": "14"},
            )

        self.saw_secret = request.headers.get("Authorization") == "Bearer test-secret-sentinel"
        path = request.url.path
        if request.method == "POST" and path == "/v2/video_generation":
            self.create_calls += 1
            task_id = f"task-{self.create_calls}"
            self.tasks[task_id] = "queued"
            payload = json.loads(request.content)
            assert payload["model"] == "MiniMax-H3"
            assert "confirmed" not in payload
            assert "client_request_id" not in payload
            return httpx.Response(200, json={"task_id": task_id})
        if request.method == "GET" and path.startswith("/v2/query/video_generation/task-"):
            self.query_calls += 1
            task_id = path.rsplit("/", 1)[-1]
            status = self.tasks[task_id]
            task = {
                "id": task_id,
                "model": "MiniMax-H3",
                "status": status,
                "created_at": 1_700_000_000,
                "updated_at": 1_700_000_010,
                "resolution": "768P",
                "duration": 4,
                "ratio": "16:9",
                "task_type": "generation",
                "modality": "video",
            }
            if status == "succeeded":
                task["content"] = {"url": "https://cdn.example.test/output.mp4?signature=private"}
            return httpx.Response(200, json={"task": task})
        if request.method == "DELETE" and path.startswith("/v2/video_generation/task-"):
            task_id = path.rsplit("/", 1)[-1]
            action = "cancelled" if self.tasks[task_id] == "queued" else "deleted"
            self.tasks[task_id] = action
            return httpx.Response(
                200, json={"task_id": task_id, "action": action, "status": action}
            )
        if request.method == "GET" and path == "/v2/query/video_generation":
            items = [
                {
                    "id": task_id,
                    "status": status,
                    "task_type": "generation",
                    "model": "MiniMax-H3",
                }
                for task_id, status in self.tasks.items()
            ]
            return httpx.Response(200, json={"items": items, "total": len(items)})
        raise AssertionError(f"Unexpected upstream request: {request.method} {request.url}")


async def submit(client, request_id: str) -> dict:
    body = generation_request(request_id)
    body["confirmed"] = True
    response = await client.post("/api/jobs", json=body)
    assert response.status_code == 201
    return response.json()


async def test_new_submissions_accumulate_and_duplicate_request_is_not_recreated(
    make_client,
) -> None:
    upstream = StatefulMiniMax()
    client = make_client(upstream)
    first = await submit(client, "request-first")
    second = await submit(client, "request-second")
    replay = await client.post(
        "/api/jobs",
        json={**generation_request("request-first"), "confirmed": True},
    )
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True
    assert replay.json()["task_id"] == first["task_id"]
    assert second["task_id"] != first["task_id"]
    assert upstream.create_calls == 2
    jobs = (await client.get("/api/jobs")).json()
    assert len(jobs) == 2
    assert all(job["status"] == "queued" for job in jobs)
    assert upstream.saw_secret is True
    assert "test-secret-sentinel" not in json.dumps(jobs)


async def test_create_requires_explicit_confirmation(make_client) -> None:
    upstream = StatefulMiniMax()
    client = make_client(upstream)
    response = await client.post("/api/jobs", json=generation_request())
    assert response.status_code == 409
    assert upstream.create_calls == 0


async def test_context_ir_requires_explicit_confirmation(make_client) -> None:
    upstream = StatefulMiniMax()
    client = make_client(upstream)
    body = generation_request("request-context-unconfirmed")
    body.pop("resolution")
    body.pop("aigc_watermark")

    response = await client.post("/api/context-ir", json=body)

    assert response.status_code == 409
    assert upstream.create_calls == 0
    assert upstream.tasks == {}


async def test_regeneration_requires_explicit_confirmation(make_client) -> None:
    upstream = StatefulMiniMax()
    client = make_client(upstream)

    response = await client.post(
        "/api/regenerations",
        json={
            "client_request_id": "request-regeneration-unconfirmed",
            "model": "MiniMax-H3",
            "source_task_id": "task-source",
            "resolution": "2K",
            "aigc_watermark": False,
            "confirmed": False,
        },
    )

    assert response.status_code == 409
    assert upstream.create_calls == 0
    assert upstream.tasks == {}


async def test_provider_upload_requires_explicit_confirmation(make_client) -> None:
    upstream = StatefulMiniMax()
    client = make_client(upstream)
    uploaded = await client.post(
        "/api/assets/upload",
        files={"file": ("reference.mp3", b"ID3-test-audio", "audio/mpeg")},
    )
    assert uploaded.status_code == 201

    response = await client.post(
        f"/api/assets/{uploaded.json()['id']}/publish",
        json={"confirmed": False},
    )

    assert response.status_code == 409
    assert upstream.create_calls == 0
    assert upstream.query_calls == 0
    assert upstream.tasks == {}


async def test_remote_task_deletion_requires_explicit_confirmation(make_client) -> None:
    upstream = StatefulMiniMax()
    client = make_client(upstream)
    task_id = (await submit(client, "request-delete-unconfirmed"))["task_id"]

    response = await client.request(
        "DELETE",
        f"/api/jobs/{task_id}/remote",
        json={"expected_status": "queued", "confirmed": False},
    )

    assert response.status_code == 409
    assert upstream.create_calls == 1
    assert upstream.query_calls == 0
    assert upstream.tasks[task_id] == "queued"


async def test_polling_sanitizes_signed_url_and_download_omits_bearer(make_client) -> None:
    upstream = StatefulMiniMax()
    client = make_client(upstream)
    task_id = (await submit(client, "request-download"))["task_id"]
    upstream.tasks[task_id] = "succeeded"
    refreshed = await client.post(f"/api/jobs/{task_id}/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["task"]["content"] == {"output_available": True}
    assert "signature=private" not in refreshed.text
    assert "signature=private" not in json.dumps((await client.get("/api/jobs")).json())

    saved = await client.post(f"/api/jobs/{task_id}/download")
    assert saved.status_code == 200
    download = await client.get(saved.json()["download_url"])
    assert download.content == b"fake-mp4-bytes"
    assert upstream.cdn_had_authorization is False


async def test_concurrent_tabs_coalesce_the_same_task_refresh(make_client) -> None:
    upstream = StatefulMiniMax()
    client = make_client(upstream)
    task_id = (await submit(client, "request-coalesced-poll"))["task_id"]

    first, second = await asyncio.gather(
        client.post(f"/api/jobs/{task_id}/refresh"),
        client.post(f"/api/jobs/{task_id}/refresh"),
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert upstream.query_calls == 1


async def test_cross_task_polling_honors_server_concurrency_cap(
    make_client,
    monkeypatch,
) -> None:
    monkeypatch.setattr(main_module, "POLL_START_INTERVAL_SECONDS", 0.0)
    upstream = StatefulMiniMax()
    release = asyncio.Event()
    saturated = asyncio.Event()
    active = 0
    maximum_active = 0
    started = 0

    async def coordinated(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active, started
        if request.method != "GET" or "/v2/query/video_generation/" not in request.url.path:
            return upstream(request)
        started += 1
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 4:
            saturated.set()
        try:
            await release.wait()
            return upstream(request)
        finally:
            active -= 1

    client = make_client(coordinated)
    task_ids = [
        (await submit(client, f"request-concurrency-{index}"))["task_id"] for index in range(5)
    ]
    refreshes = [
        asyncio.create_task(client.post(f"/api/jobs/{task_id}/refresh")) for task_id in task_ids
    ]

    await asyncio.wait_for(saturated.wait(), timeout=1)
    assert started == 4
    assert maximum_active == 4
    release.set()
    responses = await asyncio.gather(*refreshes)

    assert all(response.status_code == 200 for response in responses)
    assert upstream.query_calls == 5
    assert maximum_active == 4


async def test_cross_task_poll_starts_are_globally_paced(make_client, monkeypatch) -> None:
    interval = 0.04
    monkeypatch.setattr(main_module, "POLL_START_INTERVAL_SECONDS", interval)
    upstream = StatefulMiniMax()
    query_started_at: list[float] = []

    def timed(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/v2/query/video_generation/" in request.url.path:
            query_started_at.append(time.monotonic())
        return upstream(request)

    client = make_client(timed)
    task_ids = [(await submit(client, f"request-paced-{index}"))["task_id"] for index in range(3)]
    responses = await asyncio.gather(
        *(client.post(f"/api/jobs/{task_id}/refresh") for task_id in task_ids)
    )

    assert all(response.status_code == 200 for response in responses)
    assert len(query_started_at) == 3
    gaps = [
        right - left for left, right in zip(query_started_at, query_started_at[1:], strict=False)
    ]
    assert all(gap >= interval * 0.75 for gap in gaps)


async def test_repeated_task_poll_failure_is_coalesced(make_client) -> None:
    upstream = StatefulMiniMax()
    client = make_client(upstream)
    task_id = (await submit(client, "request-coalesced-failure"))["task_id"]

    def failing(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith(f"/{task_id}")
        upstream.query_calls += 1
        return httpx.Response(
            503,
            json={"error": {"type": "server_error", "message": "try later"}},
        )

    client._transport.app.state.provider.transport = httpx.MockTransport(failing)
    first = await client.post(f"/api/jobs/{task_id}/refresh")
    second = await client.post(f"/api/jobs/{task_id}/refresh")
    assert first.status_code == 503
    assert second.status_code == 503
    assert upstream.query_calls == 1


def test_retry_after_accepts_delta_seconds_and_http_dates() -> None:
    assert _retry_after_seconds("30") == 30
    future = format_datetime(
        datetime.now(UTC) + timedelta(seconds=60),
        usegmt=True,
    )
    assert 45 <= _retry_after_seconds(future) <= 60
    assert _retry_after_seconds("not-a-date") == 15


async def test_provider_poll_backoff_applies_across_tasks_and_tabs(make_client) -> None:
    upstream = StatefulMiniMax()
    client = make_client(upstream)
    first_id = (await submit(client, "request-rate-limit-1"))["task_id"]
    second_id = (await submit(client, "request-rate-limit-2"))["task_id"]
    original = upstream.__call__

    def rate_limited(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith(f"/{first_id}"):
            upstream.query_calls += 1
            return httpx.Response(
                429,
                json={"error": {"type": "rate_limit", "message": "slow down"}},
                headers={"Retry-After": "30"},
            )
        return original(request)

    client._transport.app.state.provider.transport = httpx.MockTransport(rate_limited)
    limited = await client.post(f"/api/jobs/{first_id}/refresh")
    globally_paused = await client.post(f"/api/jobs/{second_id}/refresh")
    assert limited.status_code == 429
    assert globally_paused.status_code == 429
    assert "globally backed off" in globally_paused.text
    assert upstream.query_calls == 1


async def test_old_active_task_becomes_locally_archivable_when_provider_forgets_it(
    make_client,
) -> None:
    upstream = StatefulMiniMax()
    client = make_client(upstream)
    task_id = (await submit(client, "request-expired-poll"))["task_id"]
    store = client._transport.app.state.store
    with sqlite3.connect(store.database_path) as db:
        db.execute(
            "UPDATE jobs SET created_at = ? WHERE task_id = ?",
            (int(time.time()) - 8 * 24 * 60 * 60, task_id),
        )

    original = upstream.__call__

    def expired(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith(f"/{task_id}"):
            upstream.query_calls += 1
            return httpx.Response(
                404,
                json={"error": {"type": "invalid_task_id", "message": "not found"}},
            )
        return original(request)

    client._transport.app.state.provider.transport = httpx.MockTransport(expired)
    refreshed = await client.post(f"/api/jobs/{task_id}/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["task"]["status"] == "unavailable"
    job = (await client.get("/api/jobs")).json()[0]
    assert job["status"] == "unavailable"
    removed = await client.request("DELETE", f"/api/jobs/{task_id}/local")
    assert removed.status_code == 200


async def test_provider_creation_time_controls_synced_task_expiry(make_client) -> None:
    task_id = "job-synced-near-expiry"
    provider_created_at = int(time.time()) - 8 * 24 * 60 * 60

    def provider_history(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v2/query/video_generation":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": task_id,
                            "status": "queued",
                            "task_type": "generation",
                            "created_at": provider_created_at,
                        }
                    ],
                    "total": 1,
                },
            )
        if request.method == "GET" and request.url.path.endswith(f"/{task_id}"):
            return httpx.Response(
                404,
                json={"error": {"type": "invalid_task_id", "message": "not found"}},
            )
        raise AssertionError(f"Unexpected upstream request: {request.method} {request.url}")

    client = make_client(provider_history)
    synced = await client.post("/api/provider/tasks?page_num=1&page_size=100")
    assert synced.status_code == 200
    local = (await client.get("/api/jobs")).json()[0]
    assert local["created_at"] == provider_created_at

    refreshed = await client.post(f"/api/jobs/{task_id}/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["task"]["status"] == "unavailable"


async def test_active_task_cannot_be_hidden_by_local_history_deletion(make_client) -> None:
    upstream = StatefulMiniMax()
    client = make_client(upstream)
    task_id = (await submit(client, "request-keep-active-visible"))["task_id"]

    blocked = await client.request("DELETE", f"/api/jobs/{task_id}/local")
    assert blocked.status_code == 409
    assert "must remain visible" in blocked.text
    assert (await client.get("/api/jobs")).json()[0]["task_id"] == task_id
    assert upstream.tasks[task_id] == "queued"


async def test_explicit_kill_only_cancels_queued_task(make_client) -> None:
    upstream = StatefulMiniMax()
    client = make_client(upstream)
    task_id = (await submit(client, "request-kill"))["task_id"]
    killed = await client.request(
        "DELETE",
        f"/api/jobs/{task_id}/remote",
        json={"expected_status": "queued", "confirmed": True},
    )
    assert killed.status_code == 200
    assert killed.json()["action"] == "cancelled"
    assert (await client.get("/api/jobs")).json()[0]["status"] == "cancelled"


async def test_running_task_cannot_be_killed(make_client) -> None:
    upstream = StatefulMiniMax()
    client = make_client(upstream)
    task_id = (await submit(client, "request-running"))["task_id"]
    upstream.tasks[task_id] = "running"
    response = await client.request(
        "DELETE",
        f"/api/jobs/{task_id}/remote",
        json={"expected_status": "queued", "confirmed": True},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["actual"] == "running"


async def test_read_only_connection_probe_discards_task_data(make_client) -> None:
    upstream = StatefulMiniMax()
    upstream.tasks["task-private"] = "succeeded"
    client = make_client(upstream)
    response = await client.post("/api/connection/test")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "authenticated": True,
        "operation": "read_only_task_list",
    }
    assert "task-private" not in response.text
