from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class AssetKind(str, Enum):
    text = "text"
    image = "image"
    video = "video"
    audio = "audio"


class AssetSource(str, Enum):
    local = "local"
    remote = "remote"
    mm_file = "mm_file"
    text = "text"


class AssetRecord(BaseModel):
    id: str
    kind: AssetKind
    name: str
    source_type: AssetSource
    mime_type: str | None = None
    size: int | None = None
    notes: str = ""
    tags: list[str] = Field(default_factory=list)
    created_at: int
    provider_file_id: str | None = None
    provider_expires_at: int | None = None
    preview_url: str | None = None
    source_url: str | None = None
    text: str | None = None


class TextAssetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=7000)
    notes: str = Field(default="", max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=20)


class RemoteAssetCreate(BaseModel):
    kind: Literal["image", "video", "audio"]
    name: str = Field(min_length=1, max_length=160)
    url: str = Field(min_length=1, max_length=4096)
    notes: str = Field(default="", max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=20)


class AssetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    notes: str | None = Field(default=None, max_length=1000)
    tags: list[str] | None = Field(default=None, max_length=20)
    text: str | None = Field(default=None, min_length=1, max_length=7000)
    url: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def one_editable_value(self) -> AssetUpdate:
        if self.text is not None and self.url is not None:
            raise ValueError("Edit either text or URL, not both")
        return self


class ImageResizeCreate(BaseModel):
    ratio: Literal["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]
    max_edge: int = Field(default=2048, ge=32, le=4096)


Role = Literal[
    "first_frame",
    "last_frame",
    "reference_image",
    "reference_video",
    "reference_audio",
    "base_video",
]


class ComposerItem(BaseModel):
    type: AssetKind
    text: str | None = Field(default=None, max_length=7000)
    asset_id: str | None = None
    url: str | None = Field(default=None, max_length=4096)
    role: Role | None = None

    @model_validator(mode="after")
    def validate_source(self) -> ComposerItem:
        sources = sum(bool(value) for value in (self.text, self.asset_id, self.url))
        if sources != 1:
            raise ValueError("Each content item must have exactly one of text, asset_id, or url")
        if self.type == AssetKind.text:
            if self.url:
                raise ValueError("Text items cannot use a URL")
            if self.role:
                raise ValueError("Text items cannot have a role")
        elif self.text:
            raise ValueError("Media items cannot contain inline text")
        return self


class VideoGenerationCreate(BaseModel):
    client_request_id: str = Field(min_length=8, max_length=128)
    model: Literal["MiniMax-H3"] = "MiniMax-H3"
    content: list[ComposerItem] = Field(min_length=1, max_length=16)
    resolution: Literal["768P", "2K"] = "768P"
    duration: int = Field(default=4, ge=4, le=15)
    ratio: Literal["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]
    callback_url: str | None = Field(default=None, max_length=4096)
    aigc_watermark: bool = False
    confirmed: bool = False


class ContextIRCreate(BaseModel):
    client_request_id: str = Field(min_length=8, max_length=128)
    model: Literal["MiniMax-H3"] = "MiniMax-H3"
    content: list[ComposerItem] = Field(min_length=1, max_length=16)
    duration: int = Field(default=4, ge=4, le=15)
    ratio: Literal["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]
    callback_url: str | None = Field(default=None, max_length=4096)
    confirmed: bool = False


class RegenerationCreate(BaseModel):
    client_request_id: str = Field(min_length=8, max_length=128)
    model: Literal["MiniMax-H3"] = "MiniMax-H3"
    source_task_id: str | None = Field(default=None, min_length=1, max_length=128)
    content: list[ComposerItem] | None = Field(default=None, min_length=1, max_length=20)
    resolution: Literal["2K"] = "2K"
    callback_url: str | None = Field(default=None, max_length=4096)
    aigc_watermark: bool = False
    confirmed: bool = False

    @model_validator(mode="after")
    def exactly_one_source(self) -> RegenerationCreate:
        if bool(self.source_task_id) == bool(self.content):
            raise ValueError("Provide exactly one of source_task_id or content")
        return self


class ProviderFilePublish(BaseModel):
    confirmed: bool = False


class RemoteTaskDelete(BaseModel):
    expected_status: Literal["queued", "succeeded", "failed"]
    confirmed: bool = False


class JobRecord(BaseModel):
    task_id: str
    operation: str
    status: str
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    created_at: int
    updated_at: int
    downloaded_filename: str | None = None


class DownloadRecord(BaseModel):
    filename: str
    size: int
    url: str
