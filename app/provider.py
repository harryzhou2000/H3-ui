from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.providers.base import ProviderError, ProviderInfo


class MiniMaxClient:
    """Purpose-built MiniMax adapter. It never accepts arbitrary upstream paths."""

    info = ProviderInfo(
        name="MiniMax",
        model="MiniMax-H3",
        api_contract="Video Generation V2",
    )

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        if not self.settings.api_key:
            raise ProviderError(
                503,
                "MINIMAX_API_KEY is not configured on the server",
                error_type="configuration_error",
            )
        return {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json_body: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        timeout = httpx.Timeout(self.settings.request_timeout_seconds, connect=15.0)
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.api_base_url,
                headers=self._headers(),
                timeout=timeout,
                transport=self.transport,
            ) as client:
                response = await client.request(
                    method, path, params=params, json=json_body, data=data, files=files
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                504,
                "MiniMax did not respond before the timeout. The submission outcome may be unknown; do not retry a create request automatically.",
                error_type="upstream_timeout",
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                502, "Could not reach MiniMax", error_type="upstream_connection_error"
            ) from exc

        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderError(
                502,
                f"MiniMax returned an unreadable response (HTTP {response.status_code})",
                error_type="invalid_upstream_response",
            ) from exc

        if not response.is_success:
            error = body.get("error", {}) if isinstance(body, dict) else {}
            raise ProviderError(
                response.status_code,
                str(error.get("message") or body.get("message") or "MiniMax request failed"),
                error_type=str(error.get("type") or "provider_error"),
                request_id=body.get("request_id") if isinstance(body, dict) else None,
                retry_after=response.headers.get("Retry-After"),
            )

        if not isinstance(body, dict):
            raise ProviderError(502, "MiniMax returned an unexpected response")

        # Legacy file endpoints can report failures inside HTTP 200.
        base_resp = body.get("base_resp")
        if isinstance(base_resp, dict) and base_resp.get("status_code") not in (None, 0, "0"):
            raise ProviderError(
                400,
                str(base_resp.get("status_msg") or "MiniMax file operation failed"),
                error_type="provider_file_error",
            )
        return body

    async def test_connection(self) -> None:
        body = await self.list_tasks(page_num=1, page_size=1)
        if not isinstance(body.get("items"), list) or not isinstance(body.get("total"), int):
            raise ProviderError(502, "MiniMax task list response did not match the V2 contract")

    async def create_video(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v2/video_generation", json_body=payload)

    async def create_context_ir(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v2/h3_context_ir", json_body=payload)

    async def regenerate_video(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v2/video_regeneration", json_body=payload)

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v2/query/video_generation/{task_id}")

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
        params: list[tuple[str, str | int]] = [
            ("page_num", page_num),
            ("page_size", page_size),
        ]
        if status:
            params.append(("filter.status", status))
        for task_id in task_ids or []:
            params.append(("filter.task_ids", task_id))
        if model:
            params.append(("filter.model", model))
        if task_type:
            params.append(("filter.task_type", task_type))
        return await self._request("GET", "/v2/query/video_generation", params=params)

    async def delete_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/v2/video_generation/{task_id}")

    async def upload_video_input(self, path: Path, filename: str, mime_type: str) -> dict[str, Any]:
        with path.open("rb") as handle:
            return await self._request(
                "POST",
                "/v1/files/upload",
                data={"purpose": "video_generation_input"},
                files={"file": (filename, handle, mime_type)},
            )

    async def list_video_inputs(self) -> dict[str, Any]:
        return await self._request(
            "GET", "/v1/files/list", params={"purpose": "video_generation_input"}
        )

    async def download_result(self, url: str, destination: Path) -> int:
        def validate_url(value: str) -> None:
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username:
                raise ProviderError(400, "MiniMax returned an unsafe download URL")
            hostname = parsed.hostname.lower()
            if hostname == "localhost" or hostname.endswith(".local"):
                raise ProviderError(400, "MiniMax returned an unsafe download URL")
            try:
                address = ipaddress.ip_address(hostname)
            except ValueError:
                return
            if not address.is_global:
                raise ProviderError(400, "MiniMax returned an unsafe download URL")

        timeout = httpx.Timeout(self.settings.download_timeout_seconds, connect=20.0)
        written = 0
        part = destination.with_suffix(destination.suffix + ".part")
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                current_url = url
                for _ in range(6):
                    validate_url(current_url)
                    async with client.stream("GET", current_url) as response:
                        if response.is_redirect:
                            location = response.headers.get("Location")
                            if not location:
                                raise ProviderError(502, "MiniMax download redirect was invalid")
                            current_url = str(response.url.join(location))
                            continue
                        response.raise_for_status()
                        declared = response.headers.get("Content-Length")
                        if declared and int(declared) > self.settings.max_download_bytes:
                            raise ProviderError(
                                413, "Generated video is larger than the local limit"
                            )
                        with part.open("wb") as handle:
                            async for chunk in response.aiter_bytes():
                                written += len(chunk)
                                if written > self.settings.max_download_bytes:
                                    raise ProviderError(
                                        413, "Generated video is larger than the local limit"
                                    )
                                handle.write(chunk)
                        break
                else:
                    raise ProviderError(502, "MiniMax download had too many redirects")
            if written == 0:
                raise ProviderError(502, "MiniMax returned an empty video file")
            part.replace(destination)
            return written
        except ProviderError:
            part.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            part.unlink(missing_ok=True)
            raise ProviderError(502, "Could not save the generated video") from exc
