from __future__ import annotations

import asyncio

import httpx


async def test_provider_sync_requires_same_origin_post(make_client) -> None:
    provider_calls = 0

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        assert request.method == "GET"
        assert request.url.path == "/v2/query/video_generation"
        provider_calls += 1
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "task-synced",
                        "status": "queued",
                        "task_type": "generation",
                    }
                ],
                "total": 1,
            },
        )

    client = make_client(provider)

    wrong_method = await client.get(
        "/api/provider/tasks",
        headers={"Origin": "https://attacker.example"},
    )
    cross_origin = await client.post(
        "/api/provider/tasks",
        headers={"Origin": "https://attacker.example"},
    )
    assert wrong_method.status_code == 405
    assert cross_origin.status_code == 403
    assert provider_calls == 0
    assert (await client.get("/api/jobs")).json() == []

    synced = await client.post("/api/provider/tasks")
    assert synced.status_code == 200
    assert provider_calls == 1
    assert (await client.get("/api/jobs")).json()[0]["task_id"] == "task-synced"


async def test_malformed_provider_status_cannot_demote_or_hide_active_job(
    make_client,
) -> None:
    task_id = "task-missing-status"

    def provider(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith(f"/{task_id}")
        return httpx.Response(
            200,
            json={"task": {"id": task_id, "task_type": "generation"}},
        )

    client = make_client(provider)
    store = client._transport.app.state.store
    store.upsert_job(task_id, "generation", "queued")

    refreshed = await client.post(f"/api/jobs/{task_id}/refresh")
    assert refreshed.status_code == 502
    assert "invalid status" in refreshed.text
    assert store.get_job(task_id)["status"] == "queued"

    hidden = await client.request("DELETE", f"/api/jobs/{task_id}/local")
    assert hidden.status_code == 409
    assert store.get_job(task_id)["status"] == "queued"


async def test_stale_sync_cannot_overwrite_successful_cancellation(make_client) -> None:
    task_id = "task-stale-sync"
    list_started = asyncio.Event()
    release_list = asyncio.Event()

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v2/query/video_generation":
            list_started.set()
            await release_list.wait()
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": task_id,
                            "status": "queued",
                            "task_type": "generation",
                        }
                    ],
                    "total": 1,
                },
            )
        if request.method == "GET" and request.url.path.endswith(f"/{task_id}"):
            return httpx.Response(
                200,
                json={
                    "task": {
                        "id": task_id,
                        "status": "queued",
                        "task_type": "generation",
                    }
                },
            )
        if request.method == "DELETE" and request.url.path.endswith(f"/{task_id}"):
            return httpx.Response(
                200,
                json={"task_id": task_id, "action": "cancelled"},
            )
        raise AssertionError(f"Unexpected upstream request: {request.method} {request.url}")

    client = make_client(provider)
    store = client._transport.app.state.store
    store.upsert_job(task_id, "generation", "queued")

    syncing = asyncio.create_task(client.post("/api/provider/tasks"))
    await asyncio.wait_for(list_started.wait(), timeout=1)
    cancelled = await client.request(
        "DELETE",
        f"/api/jobs/{task_id}/remote",
        json={"expected_status": "queued", "confirmed": True},
    )
    assert cancelled.status_code == 200
    assert store.get_job(task_id)["status"] == "cancelled"

    release_list.set()
    synced = await syncing
    assert synced.status_code == 200
    assert store.get_job(task_id)["status"] == "cancelled"


async def test_remote_cancellation_waits_for_same_task_refresh(make_client) -> None:
    task_id = "task-serialized-lifecycle"
    first_query_started = asyncio.Event()
    release_first_query = asyncio.Event()
    second_query_started = asyncio.Event()
    query_count = 0

    async def provider(request: httpx.Request) -> httpx.Response:
        nonlocal query_count
        if request.method == "GET" and request.url.path.endswith(f"/{task_id}"):
            query_count += 1
            if query_count == 1:
                first_query_started.set()
                await release_first_query.wait()
            else:
                second_query_started.set()
            return httpx.Response(
                200,
                json={
                    "task": {
                        "id": task_id,
                        "status": "queued",
                        "task_type": "generation",
                    }
                },
            )
        if request.method == "DELETE" and request.url.path.endswith(f"/{task_id}"):
            return httpx.Response(
                200,
                json={"task_id": task_id, "action": "cancelled"},
            )
        raise AssertionError(f"Unexpected upstream request: {request.method} {request.url}")

    client = make_client(provider)
    store = client._transport.app.state.store
    store.upsert_job(task_id, "generation", "queued")

    refreshing = asyncio.create_task(client.post(f"/api/jobs/{task_id}/refresh"))
    await asyncio.wait_for(first_query_started.wait(), timeout=1)
    cancelling = asyncio.create_task(
        client.request(
            "DELETE",
            f"/api/jobs/{task_id}/remote",
            json={"expected_status": "queued", "confirmed": True},
        )
    )

    try:
        await asyncio.wait_for(second_query_started.wait(), timeout=0.02)
    except TimeoutError:
        pass
    else:  # pragma: no cover - fails only when lifecycle serialization regresses
        raise AssertionError("Cancellation queried MiniMax before refresh released its task lock")

    release_first_query.set()
    refreshed, cancelled = await asyncio.gather(refreshing, cancelling)
    assert refreshed.status_code == 200
    assert cancelled.status_code == 200
    assert query_count == 2
    assert store.get_job(task_id)["status"] == "cancelled"
