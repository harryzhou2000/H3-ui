from __future__ import annotations

import asyncio
import copy
import ipaddress
import re
import time
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import uvicorn
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.db import StudioStore
from app.media import MediaService, ValidationError
from app.providers.base import ProviderError, VideoProvider
from app.providers.factory import build_provider
from app.schemas import (
    AssetRecord,
    AssetUpdate,
    ContextIRCreate,
    ImageResizeCreate,
    JobRecord,
    ProviderFilePublish,
    RegenerationCreate,
    RemoteAssetCreate,
    RemoteTaskDelete,
    TextAssetCreate,
    VideoGenerationCreate,
)

TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SAFE_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")
ACTIVE_STATUSES = {"queued", "running"}
PROVIDER_TASK_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled"}
DEFINITE_REJECTION_HTTP_CODES = {400, 401, 402, 403, 422, 429}
PROVIDER_TASK_RETENTION_SECONDS = 7 * 24 * 60 * 60
POLL_CACHE_SECONDS = 7.5
POLL_START_INTERVAL_SECONDS = 0.25
AMBIGUOUS_SUBMISSION_ERROR_TYPES = {
    "invalid_upstream_response",
    "upstream_connection_error",
    "upstream_timeout",
}


def _task_id(value: str) -> str:
    if not TASK_ID_PATTERN.fullmatch(value):
        raise HTTPException(status_code=422, detail="Invalid task ID")
    return value


def _sanitize_provider_response(body: dict[str, Any]) -> dict[str, Any]:
    """Keep signed output URLs server-side while preserving result availability."""
    safe = copy.deepcopy(body)
    tasks: list[dict[str, Any]] = []
    if isinstance(safe.get("task"), dict):
        tasks.append(safe["task"])
    if isinstance(safe.get("items"), list):
        tasks.extend(item for item in safe["items"] if isinstance(item, dict))
    for task in tasks:
        content = task.get("content")
        if isinstance(content, dict) and content.get("url"):
            content.pop("url", None)
            content["output_available"] = True
    return safe


def _validated_provider_task(
    task: Any,
    *,
    expected_task_id: str | None = None,
) -> tuple[str, str]:
    if not isinstance(task, dict):
        raise ProviderError(
            502,
            "MiniMax query response did not include a task",
            error_type="invalid_upstream_response",
        )
    task_id = str(task.get("id") or "")
    if not TASK_ID_PATTERN.fullmatch(task_id) or (
        expected_task_id is not None and task_id != expected_task_id
    ):
        raise ProviderError(
            502,
            "MiniMax task response included an invalid task ID",
            error_type="invalid_upstream_response",
        )
    status = task.get("status")
    if not isinstance(status, str) or status not in PROVIDER_TASK_STATUSES:
        raise ProviderError(
            502,
            "MiniMax task response included an invalid status",
            error_type="invalid_upstream_response",
        )
    return task_id, status


def _stream_local_file(
    request: Request,
    path: Path,
    *,
    media_type: str,
    filename: str,
    inline: bool,
) -> StreamingResponse:
    """Stream a local file with single-range support for browser audio/video seeking."""
    size = path.stat().st_size
    start, end = 0, max(0, size - 1)
    status_code = 200
    range_header = request.headers.get("Range")
    if range_header:
        match = RANGE_PATTERN.fullmatch(range_header.strip())
        if not match:
            raise HTTPException(status_code=416, detail="Unsupported byte range")
        first, last = match.groups()
        if first:
            start = int(first)
            end = int(last) if last else end
        elif last:
            suffix = int(last)
            if suffix <= 0:
                raise HTTPException(status_code=416, detail="Invalid byte range")
            start = max(0, size - suffix)
        if start >= size or end < start:
            raise HTTPException(
                status_code=416,
                detail="Byte range outside file",
                headers={"Content-Range": f"bytes */{size}"},
            )
        end = min(end, size - 1)
        status_code = 206

    length = max(0, end - start + 1)

    async def chunks():
        remaining = length
        with path.open("rb") as handle:
            handle.seek(start)
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    disposition = "inline" if inline else "attachment"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}",
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        chunks(), status_code=status_code, media_type=media_type, headers=headers
    )


def _submission_error(existing: dict[str, Any]) -> HTTPException:
    status = existing.get("status")
    if status in {"submission_unknown", "submitting"}:
        message = (
            "This request may already have reached MiniMax. Refresh the recent task list "
            "and reconcile it before creating another request."
        )
    else:
        message = f"This client request was already attempted ({status})"
    return HTTPException(
        status_code=409,
        detail={
            "message": message,
            "submission_status": status,
            "task_id": existing.get("task_id"),
        },
    )


def _submission_intent_error(existing: dict[str, Any]) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "message": (
                "This client request ID is already reserved for a different operation or request"
            ),
            "submission_status": existing.get("status"),
            "existing_operation": existing.get("operation"),
        },
    )


def _is_definite_client_rejection(exc: ProviderError) -> bool:
    """Retry only documented client rejections; unknown 4xx outcomes fail closed."""
    return (
        exc.status_code in DEFINITE_REJECTION_HTTP_CODES
        and exc.error_type not in AMBIGUOUS_SUBMISSION_ERROR_TYPES
    )


def _canonical_host_header(settings: Settings) -> str:
    host = settings.host.lower()
    rendered_host = f"[{host}]" if ":" in host else host
    if settings.port == 80:
        return rendered_host
    return f"{rendered_host}:{settings.port}"


def _retry_after_seconds(value: str | None, default: float = 15.0) -> float:
    if not value:
        return default
    try:
        return max(1.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError, OverflowError):
            return default
        return max(1.0, retry_at - time.time())


def _provider_created_at(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        timestamp = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if timestamp <= 0:
        return None
    return min(timestamp, int(time.time()))


def create_app(
    settings: Settings | None = None,
    provider: VideoProvider | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.host.lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("H3 Studio only accepts a configured loopback host")
    settings.prepare()
    store = StudioStore(settings.database_path)
    store.initialize()
    media = MediaService(settings, store)
    provider = provider or build_provider(settings)

    app = FastAPI(
        title="H3 Studio",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.store = store
    app.state.media = media
    app.state.provider = provider
    app.state.poll_task_locks = {}
    app.state.poll_semaphore = asyncio.Semaphore(4)
    app.state.poll_rate_lock = asyncio.Lock()
    app.state.poll_next_start = 0.0
    app.state.poll_backoff_until = 0.0
    app.state.poll_cache = {}
    canonical_host_header = _canonical_host_header(settings)

    def task_lock(task_id: str) -> asyncio.Lock:
        return app.state.poll_task_locks.setdefault(task_id, asyncio.Lock())

    def enforce_poll_backoff() -> None:
        remaining = app.state.poll_backoff_until - time.monotonic()
        if remaining > 0:
            raise HTTPException(
                status_code=429,
                detail={
                    "message": "MiniMax polling is globally backed off",
                    "retry_after": max(1, int(remaining) + 1),
                },
            )

    async def wait_for_poll_rate_slot() -> None:
        async with app.state.poll_rate_lock:
            delay = app.state.poll_next_start - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            app.state.poll_next_start = time.monotonic() + POLL_START_INTERVAL_SECONDS

    def note_provider_backoff(exc: ProviderError) -> None:
        if exc.status_code != 429:
            return
        retry_seconds = _retry_after_seconds(exc.retry_after)
        app.state.poll_backoff_until = max(
            app.state.poll_backoff_until,
            time.monotonic() + retry_seconds,
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Any]]):
        try:
            client_address = ipaddress.ip_address(request.client.host if request.client else "")
        except ValueError:
            return JSONResponse(status_code=403, content={"detail": "Loopback access only"})
        if not client_address.is_loopback:
            return JSONResponse(status_code=403, content={"detail": "Loopback access only"})
        host_header = request.headers.get("host", "").lower()
        if host_header != canonical_host_header:
            return JSONResponse(status_code=400, content={"detail": "Host not allowed"})
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("Origin")
            if origin:
                expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
                if origin.rstrip("/") != expected_origin.rstrip("/"):
                    return JSONResponse(status_code=403, content={"detail": "Origin not allowed"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), "
            "clipboard-read=(self), clipboard-write=(self)"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob: https:; "
            "media-src 'self' blob: http: https:; "
            "script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValidationError)
    async def local_validation_error(_: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(ProviderError)
    async def provider_error(_: Request, exc: ProviderError) -> JSONResponse:
        status = exc.status_code if 400 <= exc.status_code <= 599 else 502
        return JSONResponse(status_code=status, content={"detail": exc.public_detail()})

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "key_configured": settings.api_key_configured,
            "provider": provider.info.name,
            "model": provider.info.model,
            "api_contract": provider.info.api_contract,
        }

    @app.post("/api/connection/test")
    async def test_connection() -> dict[str, Any]:
        await provider.test_connection()
        # Deliberately discard task/account data from the read-only probe.
        return {"ok": True, "authenticated": True, "operation": "read_only_task_list"}

    @app.get("/api/assets", response_model=list[AssetRecord])
    async def list_assets() -> list[AssetRecord]:
        return [media.present_asset(asset) for asset in store.list_assets()]

    @app.post("/api/assets/text", response_model=AssetRecord, status_code=201)
    async def create_text_asset(body: TextAssetCreate) -> AssetRecord:
        asset = media.create_text_asset(body.name, body.text, body.notes, body.tags)
        return media.present_asset(asset)

    @app.post("/api/assets/remote", response_model=AssetRecord, status_code=201)
    async def create_remote_asset(body: RemoteAssetCreate) -> AssetRecord:
        asset = media.create_remote_asset(body.kind, body.name, body.url, body.notes, body.tags)
        return media.present_asset(asset)

    @app.post("/api/assets/upload", response_model=AssetRecord, status_code=201)
    async def upload_asset(
        file: UploadFile = File(...),
        name: str | None = Form(default=None, max_length=160),
        notes: str = Form(default="", max_length=1000),
        tags: str = Form(default="", max_length=1000),
    ) -> AssetRecord:
        parsed_tags = [tag.strip() for tag in tags.split(",") if tag.strip()][:20]
        asset = await media.save_upload(file, display_name=name, notes=notes, tags=parsed_tags)
        return media.present_asset(asset)

    @app.patch("/api/assets/{asset_id}", response_model=AssetRecord)
    async def update_asset(asset_id: str, body: AssetUpdate) -> AssetRecord:
        existing = store.get_asset(asset_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Asset not found")
        updates = body.model_dump(exclude_none=True)
        updated = media.update_asset(existing, updates)
        return media.present_asset(updated or existing)

    @app.post("/api/assets/{asset_id}/resize", response_model=AssetRecord, status_code=201)
    async def resize_image_asset(asset_id: str, body: ImageResizeCreate) -> AssetRecord:
        existing = store.get_asset(asset_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Asset not found")
        resized = media.create_resized_image_copy(existing, body.ratio, body.max_edge)
        return media.present_asset(resized)

    @app.get("/api/assets/{asset_id}/content")
    async def asset_content(asset_id: str, request: Request) -> StreamingResponse:
        asset = store.get_asset(asset_id)
        if not asset:
            raise HTTPException(status_code=404, detail="Asset not found")
        path = media.local_path(asset)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Asset file is missing")
        return _stream_local_file(
            request,
            path,
            media_type=asset.get("mime_type") or "application/octet-stream",
            filename=asset["name"],
            inline=asset["kind"] in {"image", "audio", "video"},
        )

    @app.delete("/api/assets/{asset_id}")
    async def delete_asset(asset_id: str) -> dict[str, Any]:
        asset = media.delete_asset(asset_id)
        if not asset:
            raise HTTPException(status_code=404, detail="Asset not found")
        return {
            "deleted": True,
            "scope": "local_only",
            "provider_file_was_not_deleted": bool(asset.get("provider_file_id")),
        }

    @app.post("/api/assets/{asset_id}/publish", response_model=AssetRecord)
    async def publish_asset(asset_id: str, body: ProviderFilePublish) -> AssetRecord:
        if not body.confirmed:
            raise HTTPException(
                status_code=409,
                detail="Confirm before sending this local file to MiniMax for seven-day storage",
            )
        asset = store.get_asset(asset_id)
        if not asset:
            raise HTTPException(status_code=404, detail="Asset not found")
        path = media.local_path(asset)
        result = await provider.upload_video_input(
            path, asset["name"], asset.get("mime_type") or "application/octet-stream"
        )
        file_info = result.get("file")
        if not isinstance(file_info, dict) or file_info.get("file_id") is None:
            raise ProviderError(502, "MiniMax upload response did not include file_id")
        updated = store.update_asset(
            asset_id,
            {
                "provider_file_id": str(file_info["file_id"]),
                "provider_expires_at": int(time.time()) + 7 * 24 * 60 * 60,
            },
        )
        return media.present_asset(updated or asset)

    @app.get("/api/provider/files")
    async def provider_files() -> dict[str, Any]:
        result = await provider.list_video_inputs()
        return {"files": result.get("files", [])}

    @app.post("/api/jobs/preview")
    async def preview_job(body: VideoGenerationCreate) -> dict[str, Any]:
        payload, size = media.generation_payload(body)
        return {
            "valid": True,
            "estimated_request_bytes": size,
            "payload": media.redacted_preview(payload),
            "billable_operation": True,
        }

    async def submit(
        *,
        client_request_id: str,
        operation: str,
        confirmed: bool,
        request_snapshot: dict[str, Any],
        payload: dict[str, Any],
        upstream: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        if not confirmed:
            raise HTTPException(
                status_code=409,
                detail="Explicit confirmation is required for this billable operation",
            )
        started, existing = store.begin_submission(client_request_id, operation, request_snapshot)
        if not started:
            same_intent = (
                existing.get("operation") == operation
                and existing.get("request") == request_snapshot
            )
            if not same_intent:
                raise _submission_intent_error(existing)
            if existing.get("status") == "submitted" and existing.get("task_id"):
                job = store.get_job(existing["task_id"])
                if job is None:
                    replay_snapshot = dict(existing.get("request") or {})
                    replay_snapshot.pop("confirmed", None)
                    job = store.upsert_job(
                        existing["task_id"],
                        str(existing.get("operation") or operation),
                        "queued",
                        request=replay_snapshot,
                        response={"task_id": existing["task_id"]},
                    )
                return {"task_id": existing["task_id"], "job": job, "replayed": True}
            raise _submission_error(existing)
        try:
            result = await upstream(payload)
        except ProviderError as exc:
            definite_rejection = _is_definite_client_rejection(exc)
            store.fail_submission(
                client_request_id,
                "rejected" if definite_rejection else "submission_unknown",
                exc.public_detail(),
            )
            raise
        task_id = str(result.get("task_id") or "")
        if not TASK_ID_PATTERN.fullmatch(task_id):
            store.fail_submission(
                client_request_id,
                "submission_unknown",
                {"message": "MiniMax response did not include a valid task_id"},
            )
            raise ProviderError(502, "MiniMax response did not include a valid task_id")
        safe_snapshot = dict(request_snapshot)
        safe_snapshot.pop("confirmed", None)
        job = store.finish_submission_with_job(
            client_request_id,
            task_id,
            operation,
            safe_snapshot,
            {"task_id": task_id},
        )
        return {"task_id": task_id, "job": job, "replayed": False}

    @app.post("/api/jobs", status_code=201)
    async def create_job(body: VideoGenerationCreate) -> dict[str, Any]:
        payload, _ = media.generation_payload(body)
        return await submit(
            client_request_id=body.client_request_id,
            operation="generation",
            confirmed=body.confirmed,
            request_snapshot=body.model_dump(mode="json"),
            payload=payload,
            upstream=provider.create_video,
        )

    @app.post("/api/context-ir/preview")
    async def preview_context_ir(body: ContextIRCreate) -> dict[str, Any]:
        payload, size = media.context_ir_payload(body)
        return {
            "valid": True,
            "estimated_request_bytes": size,
            "payload": media.redacted_preview(payload),
            "billable_operation": True,
        }

    @app.post("/api/context-ir", status_code=201)
    async def create_context_ir(body: ContextIRCreate) -> dict[str, Any]:
        payload, _ = media.context_ir_payload(body)
        return await submit(
            client_request_id=body.client_request_id,
            operation="h3_context_ir",
            confirmed=body.confirmed,
            request_snapshot=body.model_dump(mode="json"),
            payload=payload,
            upstream=provider.create_context_ir,
        )

    @app.post("/api/regenerations/preview")
    async def preview_regeneration(body: RegenerationCreate) -> dict[str, Any]:
        payload, size = media.regeneration_payload(body)
        return {
            "valid": True,
            "estimated_request_bytes": size,
            "payload": media.redacted_preview(payload),
            "billable_operation": True,
            "whitelist_note": bool(body.source_task_id),
        }

    @app.post("/api/regenerations", status_code=201)
    async def create_regeneration(body: RegenerationCreate) -> dict[str, Any]:
        payload, _ = media.regeneration_payload(body)
        return await submit(
            client_request_id=body.client_request_id,
            operation="regeneration",
            confirmed=body.confirmed,
            request_snapshot=body.model_dump(mode="json"),
            payload=payload,
            upstream=provider.regenerate_video,
        )

    @app.get("/api/jobs", response_model=list[JobRecord])
    async def list_local_jobs() -> list[dict[str, Any]]:
        return store.list_jobs()

    @app.get("/api/provider/tasks", include_in_schema=False)
    async def reject_provider_tasks_get() -> None:
        # The root static-files mount would otherwise consume this wrong-method
        # request instead of FastAPI returning a prompt API-level rejection.
        raise HTTPException(
            status_code=405,
            detail="Provider task sync requires POST",
            headers={"Allow": "POST"},
        )

    @app.post("/api/provider/tasks")
    async def list_provider_tasks(
        page_num: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        status: str | None = Query(default=None),
        task_type: str | None = Query(default=None),
    ) -> dict[str, Any]:
        result = await provider.list_tasks(
            page_num=page_num,
            page_size=page_size,
            status=status,
            task_type=task_type,
        )
        safe = _sanitize_provider_response(result)
        items = safe.get("items")
        if not isinstance(items, list):
            raise ProviderError(
                502,
                "MiniMax task-list response did not include items",
                error_type="invalid_upstream_response",
            )
        validated: list[tuple[dict[str, Any], str, str]] = []
        for task in items:
            task_id, task_status = _validated_provider_task(task)
            validated.append((task, task_id, task_status))
        for task, task_id, task_status in validated:
            async with task_lock(task_id):
                store.upsert_job(
                    task_id,
                    str(task.get("task_type") or "generation"),
                    task_status,
                    response={"task": task},
                    created_at=_provider_created_at(task.get("created_at")),
                )
        return safe

    @app.post("/api/jobs/{task_id}/refresh")
    async def refresh_job(
        task_id: str,
        force: bool = Query(default=False),
    ) -> dict[str, Any]:
        task_id = _task_id(task_id)
        request_started_at = time.monotonic()
        async with task_lock(task_id):
            cached = app.state.poll_cache.get(task_id)
            if (
                cached
                and time.monotonic() - cached["at"] < POLL_CACHE_SECONDS
                and (not force or cached["at"] >= request_started_at)
            ):
                if cached.get("error"):
                    error = cached["error"]
                    raise ProviderError(
                        error["status_code"],
                        error["message"],
                        error_type=error["error_type"],
                        request_id=error.get("request_id"),
                        retry_after=error.get("retry_after"),
                    )
                return copy.deepcopy(cached["response"])

            async with app.state.poll_semaphore:
                enforce_poll_backoff()
                await wait_for_poll_rate_slot()
                enforce_poll_backoff()
                try:
                    result = await provider.get_task(task_id)
                    safe = _sanitize_provider_response(result)
                    task = safe.get("task")
                    _, task_status = _validated_provider_task(
                        task,
                        expected_task_id=task_id,
                    )
                except ProviderError as exc:
                    note_provider_backoff(exc)
                    job = store.get_job(task_id)
                    expired_active_job = (
                        exc.status_code in {400, 404}
                        and job is not None
                        and job.get("status") in ACTIVE_STATUSES
                        and int(job.get("created_at") or 0)
                        <= int(time.time()) - PROVIDER_TASK_RETENTION_SECONDS
                    )
                    if not expired_active_job:
                        app.state.poll_cache[task_id] = {
                            "at": time.monotonic(),
                            "error": {
                                "status_code": exc.status_code,
                                "message": exc.message,
                                "error_type": exc.error_type,
                                "request_id": exc.request_id,
                                "retry_after": exc.retry_after,
                            },
                        }
                        raise
                    safe = {
                        "task": {
                            "id": task_id,
                            "status": "unavailable",
                            "task_type": job.get("operation") or "generation",
                            "error": {
                                "message": (
                                    "MiniMax no longer exposes this task after its "
                                    "seven-day query window"
                                )
                            },
                        }
                    }
                    store.upsert_job(
                        task_id,
                        str(job.get("operation") or "generation"),
                        "unavailable",
                        response=safe,
                    )
                    app.state.poll_cache[task_id] = {
                        "at": time.monotonic(),
                        "response": safe,
                    }
                    return safe

            store.upsert_job(
                task_id,
                str(task.get("task_type") or "generation"),
                task_status,
                response=safe,
                created_at=_provider_created_at(task.get("created_at")),
            )
            app.state.poll_cache[task_id] = {
                "at": time.monotonic(),
                "response": safe,
            }
            return safe

    @app.delete("/api/jobs/{task_id}/remote")
    async def delete_remote_job(task_id: str, body: RemoteTaskDelete) -> dict[str, Any]:
        task_id = _task_id(task_id)
        if not body.confirmed:
            raise HTTPException(status_code=409, detail="Remote task action must be confirmed")
        async with task_lock(task_id):
            current = await provider.get_task(task_id)
            task = current.get("task")
            _, current_status = _validated_provider_task(
                task,
                expected_task_id=task_id,
            )
            safe_current = _sanitize_provider_response(current)
            store.upsert_job(
                task_id,
                str(task.get("task_type") or "generation"),
                current_status,
                response=safe_current,
                created_at=_provider_created_at(task.get("created_at")),
            )
            app.state.poll_cache[task_id] = {
                "at": time.monotonic(),
                "response": safe_current,
            }
            if current_status != body.expected_status:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Task status changed; review it before trying again",
                        "expected": body.expected_status,
                        "actual": current_status,
                    },
                )
            if current_status not in {"queued", "succeeded", "failed"}:
                raise HTTPException(
                    status_code=409,
                    detail="Task cannot be cancelled or deleted now",
                )
            result = await provider.delete_task(task_id)
            resulting_status = "cancelled" if current_status == "queued" else "deleted"
            store.upsert_job(
                task_id,
                str(task.get("task_type") or "generation"),
                resulting_status,
                force_status=True,
            )
            app.state.poll_cache.pop(task_id, None)
            return result

    @app.delete("/api/jobs/{task_id}/local")
    async def delete_local_job(task_id: str) -> dict[str, Any]:
        task_id = _task_id(task_id)
        async with task_lock(task_id):
            job = store.get_job(task_id)
            if not job:
                raise HTTPException(status_code=404, detail="Local job not found")
            if job.get("status") in ACTIVE_STATUSES:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Active tasks must remain visible in the pool. Kill a queued task "
                        "explicitly, or wait until the task becomes terminal."
                    ),
                )
            store.delete_local_job(task_id)
            app.state.poll_cache.pop(task_id, None)
            return {"deleted": True, "scope": "local_history_only"}

    @app.post("/api/jobs/{task_id}/download")
    async def save_result(task_id: str) -> dict[str, Any]:
        task_id = _task_id(task_id)
        raw = await provider.get_task(task_id)
        task = raw.get("task")
        _, task_status = _validated_provider_task(task, expected_task_id=task_id)
        if task_status != "succeeded":
            raise HTTPException(status_code=409, detail="Task has not succeeded")
        content = task.get("content")
        output_url = content.get("url") if isinstance(content, dict) else None
        if not isinstance(output_url, str) or not output_url:
            raise ProviderError(502, "Succeeded task did not include a video URL")
        filename = f"h3-{task_id}.mp4"
        destination = settings.downloads_dir / filename
        if destination.is_file() and destination.stat().st_size:
            size = destination.stat().st_size
        else:
            size = await provider.download_result(output_url, destination)
        store.mark_downloaded(task_id, filename)
        return {
            "saved": True,
            "filename": filename,
            "size": size,
            "download_url": f"/api/downloads/{filename}",
        }

    @app.get("/api/downloads/{filename}")
    async def download_saved_file(filename: str, request: Request) -> StreamingResponse:
        if not SAFE_FILENAME_PATTERN.fullmatch(filename):
            raise HTTPException(status_code=404, detail="File not found")
        path = (settings.downloads_dir / filename).resolve()
        if path.parent != settings.downloads_dir.resolve() or not path.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return _stream_local_file(
            request, path, media_type="video/mp4", filename=filename, inline=True
        )

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


def run() -> None:
    settings = Settings.from_env()
    if settings.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("H3 Studio only binds to a loopback host")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    run()
