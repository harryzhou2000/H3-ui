from __future__ import annotations

import json

import httpx

from tests.conftest import generation_request


class StatefulMiniMax:
    def __init__(self) -> None:
        self.create_calls = 0
        self.tasks: dict[str, str] = {}
        self.saw_secret = False
        self.cdn_had_authorization = False

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


async def test_new_submissions_accumulate_and_duplicate_request_is_not_recreated(make_client) -> None:
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
