from __future__ import annotations

import base64
import ipaddress
import json
import mimetypes
import re
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import Settings
from app.db import StudioStore
from app.schemas import (
    AssetKind,
    AssetRecord,
    AssetSource,
    ComposerItem,
    ContextIRCreate,
    RegenerationCreate,
    VideoGenerationCreate,
)

MAX_REQUEST_BYTES = 64_000_000
MAX_FILE_BYTES = {
    AssetKind.image: 30_000_000,
    AssetKind.video: 50_000_000,
    AssetKind.audio: 15_000_000,
}
EXTENSIONS = {
    AssetKind.image: {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"},
    AssetKind.video: {".mp4", ".mov"},
    AssetKind.audio: {".wav", ".mp3"},
}
ROLE_KIND = {
    "first_frame": AssetKind.image,
    "last_frame": AssetKind.image,
    "reference_image": AssetKind.image,
    "reference_video": AssetKind.video,
    "reference_audio": AssetKind.audio,
    "base_video": AssetKind.video,
}
MM_FILE_PATTERN = re.compile(r"^mm_file://([A-Za-z0-9_-]+)$")
FRAME_ROLES = {"first_frame", "last_frame"}
ASPECT_RATIOS = {
    "21:9": (21, 9),
    "16:9": (16, 9),
    "4:3": (4, 3),
    "1:1": (1, 1),
    "3:4": (3, 4),
    "9:16": (9, 16),
}
MAX_CROP_PIXELS = 80_000_000
MAX_LOCAL_IMAGE_EDGE = 4096


class ValidationError(ValueError):
    pass


def crop_image_to_ratio(
    path: Path, ratio: str, *, max_edge: int = MAX_LOCAL_IMAGE_EDGE
) -> tuple[bytes, str]:
    """Center-crop locally, then downscale oversized images without stretching."""

    try:
        ratio_width, ratio_height = ASPECT_RATIOS[ratio]
    except KeyError as exc:
        raise ValidationError(f"Unsupported crop ratio: {ratio}") from exc
    if max_edge < 1:
        raise ValidationError("Local image resize limit must be positive")

    try:
        with Image.open(path) as opened:
            source_format = (opened.format or "").upper()
            opened.seek(0)
            image = ImageOps.exif_transpose(opened)
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_CROP_PIXELS:
                raise ValidationError("Image dimensions are unsafe for local cropping")

            if width * ratio_height > height * ratio_width:
                crop_width = max(1, height * ratio_width // ratio_height)
                left = (width - crop_width) // 2
                box = (left, 0, left + crop_width, height)
            else:
                crop_height = max(1, width * ratio_height // ratio_width)
                top = (height - crop_height) // 2
                box = (0, top, width, top + crop_height)
            cropped = image.crop(box)
            if max(cropped.size) > max_edge:
                cropped.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)

            output = BytesIO()
            if source_format in {"JPEG", "JPG"}:
                if cropped.mode not in {"RGB", "L"}:
                    cropped = cropped.convert("RGB")
                cropped.save(output, format="JPEG", quality=95)
                mime = "image/jpeg"
            elif source_format == "PNG":
                cropped.save(output, format="PNG")
                mime = "image/png"
            elif source_format == "WEBP":
                cropped.save(output, format="WEBP", quality=95)
                mime = "image/webp"
            else:
                raise ValidationError(
                    "Forced frame cropping supports local JPEG, PNG, and WebP images"
                )
    except ValidationError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValidationError("Local image could not be decoded for cropping") from exc
    return output.getvalue(), mime


def validate_media_url(value: str) -> tuple[AssetSource, str]:
    value = value.strip()
    if MM_FILE_PATTERN.fullmatch(value):
        return AssetSource.mm_file, value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValidationError("Use a public HTTP(S) URL or mm_file:// file ID")
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".local"):
        raise ValidationError("Local/private URLs cannot be sent to MiniMax")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValidationError("Local/private URLs cannot be sent to MiniMax")
    return AssetSource.remote, value


def validate_callback_url(value: str) -> str:
    source, normalized = validate_media_url(value)
    if source != AssetSource.remote:
        raise ValidationError("Callback URL must be a public HTTP(S) URL")
    return normalized


def infer_kind(filename: str, content_type: str | None = None) -> AssetKind:
    extension = Path(filename).suffix.lower()
    for kind, extensions in EXTENSIONS.items():
        if extension in extensions:
            return kind
    mime = (content_type or "").lower()
    if mime.startswith("image/"):
        return AssetKind.image
    if mime.startswith("video/"):
        return AssetKind.video
    if mime.startswith("audio/"):
        return AssetKind.audio
    raise ValidationError("Unsupported file type")


def canonical_mime(kind: AssetKind, filename: str, supplied: str | None) -> str:
    extension = Path(filename).suffix.lower()
    special = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".mov": "video/quicktime",
    }
    guessed = special.get(extension) or mimetypes.guess_type(filename)[0] or supplied
    if not guessed:
        guessed = f"{kind.value}/octet-stream"
    return guessed.lower()


class MediaService:
    def __init__(self, settings: Settings, store: StudioStore) -> None:
        self.settings = settings
        self.store = store

    def present_asset(self, asset: dict[str, Any]) -> AssetRecord:
        source = AssetSource(asset["source_type"])
        result = dict(asset)
        result["preview_url"] = (
            f"/api/assets/{asset['id']}/content" if source == AssetSource.local else None
        )
        result["source_url"] = (
            asset["value"] if source in {AssetSource.remote, AssetSource.mm_file} else None
        )
        result["text"] = asset["value"] if source == AssetSource.text else None
        result.pop("value", None)
        return AssetRecord.model_validate(result)

    def local_path(self, asset: dict[str, Any]) -> Path:
        if asset["source_type"] != AssetSource.local.value:
            raise ValidationError("Asset is not a local file")
        name = Path(asset["value"]).name
        path = (self.settings.assets_dir / name).resolve()
        if path.parent != self.settings.assets_dir.resolve():
            raise ValidationError("Invalid local asset path")
        return path

    async def save_upload(
        self,
        upload: UploadFile,
        *,
        display_name: str | None = None,
        notes: str = "",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        original = Path(upload.filename or "upload").name
        kind = infer_kind(original, upload.content_type)
        extension = Path(original).suffix.lower()
        if extension not in EXTENSIONS[kind]:
            raise ValidationError(
                f"Unsupported {kind.value} format. Allowed: " + ", ".join(sorted(EXTENSIONS[kind]))
            )
        asset_id = uuid.uuid4().hex
        stored_name = f"{asset_id}{extension}"
        destination = self.settings.assets_dir / stored_name
        size = 0
        try:
            with destination.open("xb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES[kind]:
                        raise ValidationError(
                            f"{kind.value.title()} exceeds the MiniMax per-file limit"
                        )
                    handle.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()
        if size == 0:
            destination.unlink(missing_ok=True)
            raise ValidationError("Uploaded file is empty")

        return self.store.insert_asset(
            {
                "id": asset_id,
                "kind": kind.value,
                "name": (display_name or original).strip()[:160],
                "source_type": AssetSource.local.value,
                "value": stored_name,
                "mime_type": canonical_mime(kind, original, upload.content_type),
                "size": size,
                "notes": notes[:1000],
                "tags": (tags or [])[:20],
                "created_at": int(time.time()),
            }
        )

    def create_text_asset(
        self, name: str, text: str, notes: str, tags: list[str]
    ) -> dict[str, Any]:
        encoded = text.encode("utf-8")
        return self.store.insert_asset(
            {
                "id": uuid.uuid4().hex,
                "kind": AssetKind.text.value,
                "name": name,
                "source_type": AssetSource.text.value,
                "value": text,
                "mime_type": "text/plain; charset=utf-8",
                "size": len(encoded),
                "notes": notes,
                "tags": tags,
                "created_at": int(time.time()),
            }
        )

    def create_remote_asset(
        self, kind: str, name: str, url: str, notes: str, tags: list[str]
    ) -> dict[str, Any]:
        source_type, normalized = validate_media_url(url)
        return self.store.insert_asset(
            {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "name": name,
                "source_type": source_type.value,
                "value": normalized,
                "mime_type": None,
                "size": None,
                "notes": notes,
                "tags": tags,
                "created_at": int(time.time()),
                "provider_file_id": (
                    MM_FILE_PATTERN.fullmatch(normalized).group(1)
                    if source_type == AssetSource.mm_file
                    else None
                ),
                "provider_expires_at": None,
            }
        )

    def create_resized_image_copy(
        self, asset: dict[str, Any], ratio: str, max_edge: int
    ) -> dict[str, Any]:
        """Create a derived local image without changing or fetching the source."""

        if asset["kind"] != AssetKind.image.value:
            raise ValidationError("Only image assets can create resized image copies")
        if asset["source_type"] != AssetSource.local.value:
            raise ValidationError("Image resizing requires a local image asset")

        source_path = self.local_path(asset)
        if not source_path.is_file():
            raise ValidationError("Local image file is missing")
        raw, mime = crop_image_to_ratio(source_path, ratio, max_edge=max_edge)
        extensions = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }
        extension = extensions[mime]
        asset_id = uuid.uuid4().hex
        stored_name = f"{asset_id}{extension}"
        destination = self.settings.assets_dir / stored_name
        try:
            with destination.open("xb") as handle:
                handle.write(raw)
            return self.store.insert_asset(
                {
                    "id": asset_id,
                    "kind": AssetKind.image.value,
                    "name": f"{asset['name']} ({ratio}, max {max_edge}px)"[:160],
                    "source_type": AssetSource.local.value,
                    "value": stored_name,
                    "mime_type": mime,
                    "size": len(raw),
                    "notes": asset.get("notes", ""),
                    "tags": list(asset.get("tags", [])),
                    "created_at": int(time.time()),
                }
            )
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def delete_asset(self, asset_id: str) -> dict[str, Any] | None:
        asset = self.store.delete_asset(asset_id)
        if asset and asset["source_type"] == AssetSource.local.value:
            self.local_path(asset).unlink(missing_ok=True)
        return asset

    def update_asset(self, asset: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any] | None:
        updates = dict(changes)
        text = updates.pop("text", None)
        url = updates.pop("url", None)
        source = AssetSource(asset["source_type"])

        if text is not None:
            if source != AssetSource.text:
                raise ValidationError("Only text-prompt assets accept text edits")
            if not text.strip():
                raise ValidationError("Text prompt cannot be empty")
            updates.update(value=text, size=len(text.encode("utf-8")))

        if url is not None:
            if source not in {AssetSource.remote, AssetSource.mm_file}:
                raise ValidationError("Only URL or file-ID assets accept source edits")
            source_type, normalized = validate_media_url(url)
            provider_match = MM_FILE_PATTERN.fullmatch(normalized)
            updates.update(
                value=normalized,
                source_type=source_type.value,
                size=None,
                provider_file_id=provider_match.group(1) if provider_match else None,
                provider_expires_at=None,
            )

        return self.store.update_asset(asset["id"], updates)

    def _asset_value(
        self, item: ComposerItem, *, frame_crop_ratio: str | None = None
    ) -> tuple[AssetKind, str, int]:
        if item.text:
            return AssetKind.text, item.text.strip(), len(item.text.encode("utf-8"))
        crop_frame = bool(
            frame_crop_ratio and item.type == AssetKind.image and item.role in FRAME_ROLES | {None}
        )
        if item.url:
            if crop_frame:
                raise ValidationError(
                    "Forced frame cropping requires a local JPEG, PNG, or WebP image"
                )
            _, value = validate_media_url(item.url)
            return item.type, value, len(value.encode("utf-8"))

        asset = self.store.get_asset(item.asset_id or "")
        if not asset:
            raise ValidationError(f"Asset {item.asset_id} was not found")
        kind = AssetKind(asset["kind"])
        if kind != item.type:
            raise ValidationError(f"Asset {item.asset_id} is not a {item.type.value}")
        source = AssetSource(asset["source_type"])
        if source == AssetSource.text:
            return kind, asset["value"].strip(), len(asset["value"].encode("utf-8"))
        if source in {AssetSource.remote, AssetSource.mm_file}:
            if crop_frame:
                raise ValidationError(
                    "Forced frame cropping requires a local JPEG, PNG, or WebP image"
                )
            _, value = validate_media_url(asset["value"])
            return kind, value, len(value.encode("utf-8"))

        # Prefer an unexpired MiniMax file reference when the user explicitly uploaded it.
        if (
            not crop_frame
            and asset.get("provider_file_id")
            and (
                not asset.get("provider_expires_at")
                or asset["provider_expires_at"] > int(time.time()) + 60
            )
        ):
            value = f"mm_file://{asset['provider_file_id']}"
            return kind, value, len(value)

        path = self.local_path(asset)
        if not path.is_file():
            raise ValidationError(f"Local file for asset {item.asset_id} is missing")
        if crop_frame:
            raw, mime = crop_image_to_ratio(path, frame_crop_ratio or "")
        else:
            raw = path.read_bytes()
            mime = asset.get("mime_type") or canonical_mime(kind, asset["name"], None)
        if kind == AssetKind.video and mime != "video/mp4":
            # MiniMax only documents Base64 for MP4; MOV must use a public/mm_file URL.
            raise ValidationError("Local MOV files must be uploaded to MiniMax before use")
        data_uri_mimes = {
            ".mp3": "audio/mp3",
            ".wav": "audio/wav",
            ".mp4": "video/mp4",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".heic": "image/heic",
            ".heif": "image/heif",
        }
        data_mime = mime if crop_frame else data_uri_mimes.get(path.suffix.lower(), mime)
        value = f"data:{data_mime};base64,{base64.b64encode(raw).decode('ascii')}"
        return kind, value, len(value)

    def build_content(
        self,
        items: list[ComposerItem],
        *,
        allow_base_video: bool = False,
        frame_crop_ratio: str | None = None,
    ) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        for item in items:
            kind, value, _ = self._asset_value(item, frame_crop_ratio=frame_crop_ratio)
            if kind == AssetKind.text:
                content.append({"type": "text", "text": value})
                continue
            role = item.role
            if role == "base_video" and not allow_base_video:
                raise ValidationError("base_video is only valid for regeneration")
            provider_type = f"{kind.value}_url"
            provider_item: dict[str, Any] = {
                "type": provider_type,
                provider_type: {"url": value},
            }
            if role:
                provider_item["role"] = role
            content.append(provider_item)
        self.validate_content(content, allow_base_video=allow_base_video)
        return content

    @staticmethod
    def validate_content(
        content: list[dict[str, Any]], *, ratio: str | None = None, allow_base_video: bool = False
    ) -> None:
        texts = [item for item in content if item["type"] == "text"]
        if len(texts) != 1 or not texts[0].get("text", "").strip():
            raise ValidationError("Exactly one non-empty text prompt is required")
        if len(texts[0]["text"]) > 7000:
            raise ValidationError("Prompt cannot exceed 7,000 characters")

        roles = [item.get("role") for item in content if item.get("role")]
        media = [item for item in content if item["type"] != "text"]
        unroled = [item for item in media if not item.get("role")]
        if unroled and not (
            len(media) == 1 and len(unroled) == 1 and unroled[0]["type"] == "image_url"
        ):
            raise ValidationError(
                "Every media item needs a role (except one default first-frame image)"
            )
        for item in content:
            role = item.get("role")
            if role and ROLE_KIND.get(role) and item["type"] != f"{ROLE_KIND[role].value}_url":
                raise ValidationError(f"Role {role} cannot be used on {item['type']}")
        if any(role in {"first_frame", "last_frame"} for role in roles) and any(
            role in {"reference_image", "reference_video", "reference_audio"} for role in roles
        ):
            raise ValidationError("Frame inputs and reference inputs cannot be mixed")
        limits = {
            "first_frame": 1,
            "last_frame": 1,
            "reference_image": 9,
            "reference_video": 3,
            "reference_audio": 3,
            "base_video": 1,
        }
        for role, limit in limits.items():
            if roles.count(role) > limit:
                raise ValidationError(f"Too many {role} inputs (maximum {limit})")
        if "reference_audio" in roles and not any(
            role in {"reference_image", "reference_video"} for role in roles
        ):
            raise ValidationError("Reference audio cannot be the only reference medium")
        if "base_video" in roles:
            if not allow_base_video or roles.count("base_video") != 1:
                raise ValidationError("Regeneration requires exactly one base_video")
        elif allow_base_video:
            raise ValidationError("Regeneration content requires one base_video")

        frame_mode = bool(unroled) or any(role in {"first_frame", "last_frame"} for role in roles)
        if not media and ratio == "adaptive":
            raise ValidationError("Text-to-video ratio must be a concrete aspect ratio")
        if frame_mode and ratio not in (None, "adaptive"):
            raise ValidationError("Frame-based generation uses the adaptive ratio")

    @staticmethod
    def request_size(payload: dict[str, Any]) -> int:
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > MAX_REQUEST_BYTES:
            raise ValidationError(
                "Request exceeds 64 MB. Upload local media to MiniMax and use mm_file:// references."
            )
        return size

    @staticmethod
    def redacted_preview(payload: dict[str, Any]) -> dict[str, Any]:
        safe = json.loads(json.dumps(payload))
        for item in safe.get("content", []):
            key = item.get("type")
            nested = item.get(key)
            if isinstance(nested, dict) and str(nested.get("url", "")).startswith("data:"):
                header = nested["url"].split(",", 1)[0]
                nested["url"] = f"{header},<local data omitted>"
        return safe

    def generation_payload(self, request: VideoGenerationCreate) -> tuple[dict[str, Any], int]:
        frame_mode = any(
            item.type == AssetKind.image and item.role in FRAME_ROLES | {None}
            for item in request.content
        )
        crop_ratio = request.ratio if frame_mode and request.ratio != "adaptive" else None
        provider_ratio = "adaptive" if frame_mode else request.ratio
        content = self.build_content(request.content, frame_crop_ratio=crop_ratio)
        self.validate_content(content, ratio=provider_ratio)
        payload: dict[str, Any] = {
            "model": request.model,
            "content": content,
            "resolution": request.resolution,
            "duration": request.duration,
            "ratio": provider_ratio,
            "aigc_watermark": request.aigc_watermark,
        }
        if request.callback_url:
            payload["callback_url"] = validate_callback_url(request.callback_url)
        return payload, self.request_size(payload)

    def context_ir_payload(self, request: ContextIRCreate) -> tuple[dict[str, Any], int]:
        frame_mode = any(
            item.type == AssetKind.image and item.role in FRAME_ROLES | {None}
            for item in request.content
        )
        crop_ratio = request.ratio if frame_mode and request.ratio != "adaptive" else None
        provider_ratio = "adaptive" if frame_mode else request.ratio
        content = self.build_content(request.content, frame_crop_ratio=crop_ratio)
        self.validate_content(content, ratio=provider_ratio)
        payload: dict[str, Any] = {
            "model": request.model,
            "content": content,
            "duration": request.duration,
            "ratio": provider_ratio,
        }
        if request.callback_url:
            payload["callback_url"] = validate_callback_url(request.callback_url)
        return payload, self.request_size(payload)

    def regeneration_payload(self, request: RegenerationCreate) -> tuple[dict[str, Any], int]:
        payload: dict[str, Any] = {
            "model": request.model,
            "resolution": request.resolution,
            "aigc_watermark": request.aigc_watermark,
        }
        if request.source_task_id:
            payload["source_task_id"] = request.source_task_id
        else:
            payload["content"] = self.build_content(request.content or [], allow_base_video=True)
        if request.callback_url:
            payload["callback_url"] = validate_callback_url(request.callback_url)
        return payload, self.request_size(payload)
