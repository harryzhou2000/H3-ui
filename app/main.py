from __future__ import annotations

import copy
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlparse

import httpx
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
from app.provider import MiniMaxClient, ProviderError
from app.schemas import (
    AssetRecord,
    AssetUpdate,
    ContextIRCreate,
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
    if status == "submission_unknown":
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


def create_app(
    settings: Settings | None = None,
    provider: MiniMaxClient | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.prepare()
    store = StudioStore(settings.database_path)
    store.initialize()
    media = MediaService(settings, store)
    provider = provider or MiniMaxClient(settings)

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

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Any]]):
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
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
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
            "provider": "MiniMax",
            "model": "MiniMax-H3",
            "api_contract": "Video Generation V2",
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
        asset = media.create_remote_asset(
            body.kind, body.name, body.url, body.notes, body.tags
        )
        return media.present_asset(asset)

    @app.post("/api/assets/upload", response_model=AssetRecord, status_code=201)
    async def upload_asset(
        file: UploadFile = File(...),
        name: str | None = Form(default=None, max_length=160),
        notes: str = Form(default="", max_length=1000),
        tags: str = Form(default="", max_length=1000),
    ) -> AssetRecord:
        parsed_tags = [tag.strip() for tag in tags.split(",") if tag.strip()][:20]
        asset = await media.save_upload(
            file, display_name=name, notes=notes, tags=parsed_tags
        )
        return media.present_asset(asset)

    @app.patch("/api/assets/{asset_id}", response_model=AssetRecord)
    async def update_asset(asset_id: str, body: AssetUpdate) -> AssetRecord:
        existing = store.get_asset(asset_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Asset not found")
        updates = body.model_dump(exclude_none=True)
        updated = store.update_asset(asset_id, updates)
        return media.present_asset(updated or existing)

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
        started, existing = store.begin_submission(
            client_request_id, operation, request_snapshot
        )
        if not started:
            if existing.get("status") == "submitted" and existing.get("task_id"):
                job = store.get_job(existing["task_id"])
                return {"task_id": existing["task_id"], "job": job, "replayed": True}
            raise _submission_error(existing)
        try:
            result = await upstream(payload)
        except ProviderError as exc:
            unknown = exc.error_type in {"upstream_timeout", "upstream_connection_error"}
            store.fail_submission(
                client_request_id,
                "submission_unknown" if unknown else "rejected",
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
        store.finish_submission(client_request_id, task_id)
        safe_snapshot = dict(request_snapshot)
        safe_snapshot.pop("confirmed", None)
        job = store.upsert_job(
            task_id, operation, "queued", request=safe_snapshot, response={"task_id": task_id}
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

    @app.get("/api/provider/tasks")
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
        for task in safe.get("items", []):
            if not isinstance(task, dict) or not task.get("id"):
                continue
            store.upsert_job(
                str(task["id"]),
                str(task.get("task_type") or "generation"),
                str(task.get("status") or "unknown"),
                response={"task": task},
            )
        return safe

    @app.post("/api/jobs/{task_id}/refresh")
    async def refresh_job(task_id: str) -> dict[str, Any]:
        task_id = _task_id(task_id)
        result = await provider.get_task(task_id)
        safe = _sanitize_provider_response(result)
        task = safe.get("task")
        if not isinstance(task, dict):
            raise ProviderError(502, "MiniMax query response did not include task")
        store.upsert_job(
            task_id,
            str(task.get("task_type") or "generation"),
            str(task.get("status") or "unknown"),
            response=safe,
        )
        return safe

    @app.delete("/api/jobs/{task_id}/remote")
    async def delete_remote_job(task_id: str, body: RemoteTaskDelete) -> dict[str, Any]:
        task_id = _task_id(task_id)
        if not body.confirmed:
            raise HTTPException(status_code=409, detail="Remote task action must be confirmed")
        current = await provider.get_task(task_id)
        task = current.get("task")
        if not isinstance(task, dict):
            raise ProviderError(502, "MiniMax query response did not include task")
        current_status = str(task.get("status") or "")
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
            raise HTTPException(status_code=409, detail="Task cannot be cancelled or deleted now")
        result = await provider.delete_task(task_id)
        resulting_status = str(result.get("status") or result.get("action") or "deleted")
        store.upsert_job(task_id, str(task.get("task_type") or "generation"), resulting_status)
        return result

    @app.delete("/api/jobs/{task_id}/local")
    async def delete_local_job(task_id: str) -> dict[str, Any]:
        task_id = _task_id(task_id)
        job = store.get_job(task_id)
        if not job:
            raise HTTPException(status_code=404, detail="Local job not found")
        store.delete_local_job(task_id)
        return {"deleted": True, "scope": "local_history_only"}

    @app.post("/api/jobs/{task_id}/download")
    async def save_result(task_id: str) -> dict[str, Any]:
        task_id = _task_id(task_id)
        raw = await provider.get_task(task_id)
        task = raw.get("task")
        if not isinstance(task, dict):
            raise ProviderError(502, "MiniMax query response did not include task")
        if task.get("status") != "succeeded":
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
            request, path, media_type="video/mp4", filename=filename, inline=False
        )

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


app = create_app()


def run() -> None:
    settings = Settings.from_env()
    if settings.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("H3 Studio only binds to a loopback host")
    uvicorn.run(app, host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    run()
