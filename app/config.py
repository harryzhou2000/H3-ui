from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OFFICIAL_MINIMAX_API_BASE_URL = "https://api.minimaxi.com"


def _resolve_path(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    data_dir: Path
    api_key: str
    api_base_url: str
    host: str = "127.0.0.1"
    port: int = 8000
    request_timeout_seconds: float = 60.0
    download_timeout_seconds: float = 300.0
    max_download_bytes: int = 1_073_741_824

    @property
    def assets_dir(self) -> Path:
        return self.data_dir / "assets"

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "studio.db"

    @property
    def api_key_configured(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        root = PROJECT_ROOT
        load_dotenv(env_file or root / ".env", override=False)
        data_dir = _resolve_path(os.getenv("H3_STUDIO_DATA_DIR", "./data"), root)
        configured_base_url = os.getenv(
            "MINIMAX_API_BASE_URL", OFFICIAL_MINIMAX_API_BASE_URL
        ).rstrip("/")
        if configured_base_url != OFFICIAL_MINIMAX_API_BASE_URL:
            raise RuntimeError(
                "MINIMAX_API_BASE_URL must remain pinned to https://api.minimaxi.com "
                "when loading the production API key"
            )
        return cls(
            project_root=root,
            data_dir=data_dir,
            api_key=os.getenv("MINIMAX_API_KEY", "").strip(),
            api_base_url=OFFICIAL_MINIMAX_API_BASE_URL,
            host=os.getenv("H3_STUDIO_HOST", "127.0.0.1"),
            port=int(os.getenv("H3_STUDIO_PORT", "8000")),
            request_timeout_seconds=float(
                os.getenv("H3_STUDIO_REQUEST_TIMEOUT_SECONDS", "60")
            ),
            download_timeout_seconds=float(
                os.getenv("H3_STUDIO_DOWNLOAD_TIMEOUT_SECONDS", "300")
            ),
        )

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
