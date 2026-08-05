from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from threading import Barrier

import httpx
import pytest

from app.db import StudioStore
from tests.conftest import generation_request


class AmbiguousThenSuccess:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v2/video_generation"
        self.calls += 1
        if self.calls > 1:
            return httpx.Response(200, json={"task_id": "should-not-be-created"})
        if self.outcome == "timeout":
            raise httpx.ReadTimeout("upstream timed out", request=request)
        if self.outcome == "connection_error":
            raise httpx.ConnectError("upstream connection failed", request=request)
        if self.outcome == "http_408":
            return httpx.Response(
                408,
                json={"error": {"type": "request_timeout", "message": "timed out"}},
            )
        if self.outcome == "http_503":
            return httpx.Response(
                503,
                json={"error": {"type": "server_error", "message": "try later"}},
            )
        if self.outcome == "invalid_json":
            return httpx.Response(200, content=b"not-json")
        if self.outcome == "missing_task_id":
            return httpx.Response(200, json={"unexpected": True})
        raise AssertionError(f"Unknown test outcome: {self.outcome}")


class RejectThenSuccess:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v2/video_generation"
        self.calls += 1
        if self.calls == 1:
            return httpx.Response(
                self.status_code,
                json={
                    "error": {
                        "type": "invalid_request",
                        "message": "definitely rejected",
                    }
                },
            )
        return httpx.Response(200, json={"task_id": "task-after-retry"})


def confirmed_generation(request_id: str) -> dict:
    return {**generation_request(request_id), "confirmed": True}


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        ("timeout", 504),
        ("connection_error", 502),
        ("http_408", 408),
        ("http_503", 503),
        ("invalid_json", 502),
        ("missing_task_id", 502),
    ],
)
async def test_ambiguous_create_outcome_permanently_blocks_same_id_retry(
    make_client, outcome: str, expected_status: int
) -> None:
    upstream = AmbiguousThenSuccess(outcome)
    client = make_client(upstream)
    body = confirmed_generation(f"ambiguous-{outcome}")

    first = await client.post("/api/jobs", json=body)
    assert first.status_code == expected_status

    retry = await client.post("/api/jobs", json=body)
    assert retry.status_code == 409
    assert retry.json()["detail"]["submission_status"] == "submission_unknown"
    assert upstream.calls == 1


@pytest.mark.parametrize("status_code", [400, 401, 402, 403, 422, 429])
async def test_definite_4xx_rejection_can_retry_same_intent_with_same_id(
    make_client, status_code: int
) -> None:
    upstream = RejectThenSuccess(status_code)
    client = make_client(upstream)
    body = confirmed_generation(f"rejected-{status_code}")

    rejected = await client.post("/api/jobs", json=body)
    assert rejected.status_code == status_code

    retried = await client.post("/api/jobs", json=body)
    assert retried.status_code == 201
    assert retried.json()["task_id"] == "task-after-retry"
    assert retried.json()["replayed"] is False

    replayed = await client.post("/api/jobs", json=body)
    assert replayed.status_code == 201
    assert replayed.json()["replayed"] is True
    assert upstream.calls == 2


@pytest.mark.parametrize("status_code", [404, 408, 409, 425, 499])
async def test_undocumented_or_ambiguous_4xx_outcome_blocks_retry(
    make_client, status_code: int
) -> None:
    upstream = RejectThenSuccess(status_code)
    client = make_client(upstream)
    body = confirmed_generation(f"unknown-4xx-{status_code}")

    first = await client.post("/api/jobs", json=body)
    assert first.status_code == status_code
    retry = await client.post("/api/jobs", json=body)
    assert retry.status_code == 409
    assert retry.json()["detail"]["submission_status"] == "submission_unknown"
    assert upstream.calls == 1


async def test_rejected_id_can_only_retry_the_exact_original_request(make_client) -> None:
    upstream = RejectThenSuccess(422)
    client = make_client(upstream)
    original = confirmed_generation("rejected-request-mismatch")

    rejected = await client.post("/api/jobs", json=original)
    assert rejected.status_code == 422

    changed = {**original, "duration": 5}
    conflict = await client.post("/api/jobs", json=changed)
    assert conflict.status_code == 409
    assert "different operation or request" in conflict.json()["detail"]["message"]
    assert upstream.calls == 1

    retried = await client.post("/api/jobs", json=original)
    assert retried.status_code == 201
    assert upstream.calls == 2


async def test_submitted_id_rejects_different_request_and_operation(make_client) -> None:
    upstream = RejectThenSuccess(422)
    upstream.calls = 1  # Make the first request succeed.
    client = make_client(upstream)
    original = confirmed_generation("submitted-intent-mismatch")

    submitted = await client.post("/api/jobs", json=original)
    assert submitted.status_code == 201
    assert upstream.calls == 2

    changed = await client.post("/api/jobs", json={**original, "duration": 5})
    assert changed.status_code == 409
    assert changed.json()["detail"]["existing_operation"] == "generation"

    context_request = {
        "client_request_id": original["client_request_id"],
        "model": "MiniMax-H3",
        "content": original["content"],
        "duration": 4,
        "ratio": "16:9",
        "confirmed": True,
    }
    changed_operation = await client.post("/api/context-ir", json=context_request)
    assert changed_operation.status_code == 409
    assert changed_operation.json()["detail"]["existing_operation"] == "generation"
    assert upstream.calls == 2


def test_only_one_concurrent_retry_reopens_a_rejected_submission(tmp_path: Path) -> None:
    store = StudioStore(tmp_path / "studio.sqlite3")
    store.initialize()
    original = {"content": [{"type": "text", "text": "kite"}], "duration": 4}
    started, _ = store.begin_submission("concurrent-retry", "generation", original)
    assert started is True
    store.fail_submission("concurrent-retry", "rejected", {"http_code": 422})

    workers = 8
    barrier = Barrier(workers)

    def retry() -> bool:
        barrier.wait()
        reordered = {"duration": 4, "content": original["content"]}
        reopened, _ = store.begin_submission("concurrent-retry", "generation", reordered)
        return reopened

    with ThreadPoolExecutor(max_workers=workers) as executor:
        outcomes = list(executor.map(lambda _: retry(), range(workers)))

    assert outcomes.count(True) == 1
    current = store.get_submission("concurrent-retry")
    assert current is not None
    assert current["status"] == "submitting"
    assert current["error"] is None


def test_provider_acceptance_and_pool_insert_roll_back_together(tmp_path: Path) -> None:
    store = StudioStore(tmp_path / "studio.sqlite3")
    store.initialize()
    request = {"content": [{"type": "text", "text": "kite"}], "duration": 4}
    started, _ = store.begin_submission("atomic-submit", "generation", request)
    assert started is True

    with sqlite3.connect(store.database_path) as db:
        db.execute(
            """
            CREATE TRIGGER reject_job_insert BEFORE INSERT ON jobs
            BEGIN
                SELECT RAISE(ABORT, 'injected job write failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected job write failure"):
        store.finish_submission_with_job(
            "atomic-submit",
            "task-atomic",
            "generation",
            request,
            {"task_id": "task-atomic"},
        )

    submission = store.get_submission("atomic-submit")
    assert submission is not None
    assert submission["status"] == "submitting"
    assert submission["task_id"] is None
    assert store.get_job("task-atomic") is None
