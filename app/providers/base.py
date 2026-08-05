from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class ProviderError(Exception):
    """A sanitized provider failure that is safe to expose through the local API."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        error_type: str = "provider_error",
        request_id: str | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.request_id = request_id
        self.retry_after = retry_after

    def public_detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "type": self.error_type,
            "message": self.message,
            "http_code": self.status_code,
        }
        if self.request_id:
            detail["request_id"] = self.request_id
        if self.retry_after:
            detail["retry_after"] = self.retry_after
        return detail


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    """Stable, non-secret provider metadata suitable for the health endpoint."""

    name: str
    model: str
    api_contract: str


@runtime_checkable
class VideoProvider(Protocol):
    """Normalized operations required by the H3 Studio application layer."""

    info: ProviderInfo

    async def test_connection(self) -> None: ...

    async def create_video(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def create_context_ir(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def regenerate_video(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def get_task(self, task_id: str) -> dict[str, Any]: ...

    async def list_tasks(
        self,
        *,
        page_num: int = 1,
        page_size: int = 20,
        status: str | None = None,
        task_ids: list[str] | None = None,
        model: str | None = None,
        task_type: str | None = None,
    ) -> dict[str, Any]: ...

    async def delete_task(self, task_id: str) -> dict[str, Any]: ...

    async def upload_video_input(
        self, path: Path, filename: str, mime_type: str
    ) -> dict[str, Any]: ...

    async def list_video_inputs(self) -> dict[str, Any]: ...

    async def download_result(self, url: str, destination: Path) -> int: ...
