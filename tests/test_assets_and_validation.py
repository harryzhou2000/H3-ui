from __future__ import annotations

from io import BytesIO

import httpx
from PIL import Image

from app.media import crop_image_to_ratio
from tests.conftest import generation_request


def no_upstream(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"Unexpected upstream request: {request.method} {request.url}")


def png_bytes(size: tuple[int, int] = (40, 30)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, (30, 90, 150)).save(output, format="PNG")
    return output.getvalue()


async def test_health_never_returns_secret(make_client) -> None:
    client = make_client(no_upstream)
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["key_configured"] is True
    assert "test-secret-sentinel" not in response.text


async def test_text_and_remote_assets_are_organized_locally(make_client) -> None:
    client = make_client(no_upstream)
    text = await client.post(
        "/api/assets/text",
        json={"name": "Opening shot", "text": "A slow dolly through rain.", "tags": ["noir"]},
    )
    assert text.status_code == 201
    assert text.json()["kind"] == "text"
    assert text.json()["text"] == "A slow dolly through rain."

    remote = await client.post(
        "/api/assets/remote",
        json={
            "kind": "image",
            "name": "First frame",
            "url": "https://cdn.example.test/frame.png",
            "tags": [],
        },
    )
    assert remote.status_code == 201
    assert remote.json()["source_type"] == "remote"
    assert len((await client.get("/api/assets")).json()) == 2


async def test_every_asset_source_can_be_renamed(make_client) -> None:
    client = make_client(no_upstream)
    text = (
        await client.post("/api/assets/text", json={"name": "Text", "text": "Prompt", "tags": []})
    ).json()
    remote = (
        await client.post(
            "/api/assets/remote",
            json={"kind": "image", "name": "Remote", "url": "https://cdn.test/frame.png"},
        )
    ).json()
    local = (
        await client.post(
            "/api/assets/upload",
            files={"file": ("frame.png", png_bytes(), "image/png")},
        )
    ).json()

    edits = (
        {"name": "Renamed 1", "text": "Edited prompt body"},
        {"name": "Renamed 2", "url": "mm_file://edited-file-id"},
        {"name": "Renamed 3", "notes": "Local metadata", "tags": ["edited"]},
    )
    for asset, edit in zip((text, remote, local), edits, strict=True):
        response = await client.patch(f"/api/assets/{asset['id']}", json=edit)
        assert response.status_code == 200
        assert response.json()["name"] == edit["name"]

    assert {asset["name"] for asset in (await client.get("/api/assets")).json()} == {
        "Renamed 1",
        "Renamed 2",
        "Renamed 3",
    }
    edited = {asset["name"]: asset for asset in (await client.get("/api/assets")).json()}
    assert edited["Renamed 1"]["text"] == "Edited prompt body"
    assert edited["Renamed 2"]["source_type"] == "mm_file"
    assert edited["Renamed 2"]["source_url"] == "mm_file://edited-file-id"
    assert edited["Renamed 3"]["notes"] == "Local metadata"
    assert edited["Renamed 3"]["tags"] == ["edited"]


async def test_asset_editor_rejects_value_edits_for_the_wrong_source_type(make_client) -> None:
    client = make_client(no_upstream)
    local = (
        await client.post(
            "/api/assets/upload",
            files={"file": ("frame.png", png_bytes(), "image/png")},
        )
    ).json()
    remote = (
        await client.post(
            "/api/assets/remote",
            json={"kind": "image", "name": "Remote", "url": "https://cdn.test/frame.png"},
        )
    ).json()

    local_url = await client.patch(
        f"/api/assets/{local['id']}", json={"url": "https://cdn.test/replacement.png"}
    )
    remote_text = await client.patch(f"/api/assets/{remote['id']}", json={"text": "not an image"})
    remote_private = await client.patch(
        f"/api/assets/{remote['id']}", json={"url": "http://127.0.0.1/private.png"}
    )

    assert local_url.status_code == 422
    assert "Only URL or file-ID assets" in local_url.text
    assert remote_text.status_code == 422
    assert "Only text-prompt assets" in remote_text.text
    assert remote_private.status_code == 422
    assert "private" in remote_private.text.lower()


async def test_local_image_editor_creates_a_distinct_resized_copy(make_client) -> None:
    client = make_client(no_upstream)
    source_bytes = png_bytes((96, 60))
    original = (
        await client.post(
            "/api/assets/upload",
            files={"file": ("wide.png", source_bytes, "image/png")},
            data={"name": "Wide frame", "notes": "Keep this", "tags": "hero, blue"},
        )
    ).json()

    response = await client.post(
        f"/api/assets/{original['id']}/resize",
        json={"ratio": "1:1", "max_edge": 32},
    )

    assert response.status_code == 201
    resized = response.json()
    assert resized["id"] != original["id"]
    assert resized["source_type"] == "local"
    assert resized["mime_type"] == "image/png"
    assert resized["notes"] == "Keep this"
    assert resized["tags"] == ["hero", "blue"]
    assert "1:1" in resized["name"]
    resized_content = await client.get(resized["preview_url"])
    with Image.open(BytesIO(resized_content.content)) as image:
        assert image.size == (32, 32)

    original_content = await client.get(original["preview_url"])
    assert original_content.content == source_bytes
    assert len((await client.get("/api/assets")).json()) == 2


async def test_local_image_resize_rejects_remote_and_non_image_assets(make_client) -> None:
    client = make_client(no_upstream)
    remote = (
        await client.post(
            "/api/assets/remote",
            json={"kind": "image", "name": "Remote", "url": "https://cdn.test/frame.png"},
        )
    ).json()
    audio = (
        await client.post(
            "/api/assets/upload",
            files={"file": ("tone.mp3", b"ID3-test-audio", "audio/mpeg")},
        )
    ).json()

    remote_response = await client.post(
        f"/api/assets/{remote['id']}/resize", json={"ratio": "16:9", "max_edge": 2048}
    )
    audio_response = await client.post(
        f"/api/assets/{audio['id']}/resize", json={"ratio": "16:9", "max_edge": 2048}
    )

    assert remote_response.status_code == 422
    assert "requires a local image" in remote_response.text
    assert audio_response.status_code == 422
    assert "Only image assets" in audio_response.text
    assert len((await client.get("/api/assets")).json()) == 2


async def test_local_image_resize_validates_ratio_and_size_before_work(make_client) -> None:
    client = make_client(no_upstream)
    original = (
        await client.post(
            "/api/assets/upload",
            files={"file": ("wide.png", png_bytes(), "image/png")},
        )
    ).json()

    adaptive = await client.post(
        f"/api/assets/{original['id']}/resize",
        json={"ratio": "adaptive", "max_edge": 2048},
    )
    oversized = await client.post(
        f"/api/assets/{original['id']}/resize",
        json={"ratio": "16:9", "max_edge": 4097},
    )

    assert adaptive.status_code == 422
    assert oversized.status_code == 422
    assert len((await client.get("/api/assets")).json()) == 1


async def test_local_upload_and_content_round_trip(make_client) -> None:
    client = make_client(no_upstream)
    created = await client.post(
        "/api/assets/upload",
        files={"file": ("tone.mp3", b"ID3-test-audio", "audio/mpeg")},
    )
    assert created.status_code == 201
    asset = created.json()
    assert asset["kind"] == "audio"
    assert asset["size"] == len(b"ID3-test-audio")
    content = await client.get(asset["preview_url"])
    assert content.status_code == 200
    assert content.content == b"ID3-test-audio"
    partial = await client.get(asset["preview_url"], headers={"Range": "bytes=4-7"})
    assert partial.status_code == 206
    assert partial.content == b"test"
    assert partial.headers["content-range"] == "bytes 4-7/14"


async def test_preview_builds_exact_text_payload_without_upstream_call(make_client) -> None:
    client = make_client(no_upstream)
    response = await client.post("/api/jobs/preview", json=generation_request())
    assert response.status_code == 200
    preview = response.json()
    assert preview["valid"] is True
    assert preview["billable_operation"] is True
    assert preview["payload"] == {
        "model": "MiniMax-H3",
        "content": [{"type": "text", "text": "A paper kite rises over a quiet salt flat."}],
        "resolution": "768P",
        "duration": 4,
        "ratio": "16:9",
        "aigc_watermark": False,
    }


async def test_text_only_adaptive_ratio_is_blocked(make_client) -> None:
    client = make_client(no_upstream)
    body = generation_request()
    body["ratio"] = "adaptive"
    response = await client.post("/api/jobs/preview", json=body)
    assert response.status_code == 422
    assert "concrete aspect ratio" in response.text


def test_center_crop_preserves_pixels_and_reaches_requested_ratio(tmp_path) -> None:
    source = tmp_path / "wide.png"
    source.write_bytes(png_bytes((48, 30)))

    cropped, mime = crop_image_to_ratio(source, "16:9")

    with Image.open(BytesIO(cropped)) as image:
        assert image.size == (48, 27)
    assert mime == "image/png"


def test_center_crop_downscales_oversized_images_locally(tmp_path) -> None:
    source = tmp_path / "wide.png"
    source.write_bytes(png_bytes((48, 30)))

    resized, mime = crop_image_to_ratio(source, "16:9", max_edge=32)

    with Image.open(BytesIO(resized)) as image:
        assert image.size == (32, 18)
    assert mime == "image/png"


async def test_concrete_frame_ratio_crops_local_image_and_keeps_provider_adaptive(
    make_client,
) -> None:
    client = make_client(no_upstream)
    frame = (
        await client.post(
            "/api/assets/upload",
            files={"file": ("frame.png", png_bytes(), "image/png")},
        )
    ).json()
    body = generation_request()
    body["ratio"] = "16:9"
    body["content"].append({"type": "image", "asset_id": frame["id"], "role": "first_frame"})

    response = await client.post("/api/jobs/preview", json=body)

    assert response.status_code == 200
    payload = response.json()["payload"]
    assert payload["ratio"] == "adaptive"
    assert payload["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,<local data omitted>"
    )


async def test_forced_frame_crop_rejects_remote_images_without_fetching_them(make_client) -> None:
    client = make_client(no_upstream)
    frame = (
        await client.post(
            "/api/assets/remote",
            json={"kind": "image", "name": "Remote", "url": "https://cdn.test/frame.png"},
        )
    ).json()
    body = generation_request()
    body["ratio"] = "1:1"
    body["content"].append({"type": "image", "asset_id": frame["id"], "role": "first_frame"})

    response = await client.post("/api/jobs/preview", json=body)

    assert response.status_code == 422
    assert "requires a local JPEG, PNG, or WebP" in response.text


async def test_frame_and_reference_modes_cannot_mix(make_client) -> None:
    client = make_client(no_upstream)
    first = (
        await client.post(
            "/api/assets/remote",
            json={"kind": "image", "name": "Frame", "url": "https://cdn.test/a.png"},
        )
    ).json()
    video = (
        await client.post(
            "/api/assets/remote",
            json={"kind": "video", "name": "Motion", "url": "https://cdn.test/a.mp4"},
        )
    ).json()
    body = generation_request()
    body["ratio"] = "adaptive"
    body["content"].extend(
        [
            {"type": "image", "asset_id": first["id"], "role": "first_frame"},
            {"type": "video", "asset_id": video["id"], "role": "reference_video"},
        ]
    )
    response = await client.post("/api/jobs/preview", json=body)
    assert response.status_code == 422
    assert "cannot be mixed" in response.text


async def test_reference_audio_cannot_be_only_reference(make_client) -> None:
    client = make_client(no_upstream)
    audio = (
        await client.post(
            "/api/assets/remote",
            json={"kind": "audio", "name": "Voice", "url": "https://cdn.test/voice.mp3"},
        )
    ).json()
    body = generation_request()
    body["ratio"] = "adaptive"
    body["content"].append({"type": "audio", "asset_id": audio["id"], "role": "reference_audio"})
    response = await client.post("/api/jobs/preview", json=body)
    assert response.status_code == 422
    assert "cannot be the only reference" in response.text


async def test_private_remote_url_is_rejected(make_client) -> None:
    client = make_client(no_upstream)
    response = await client.post(
        "/api/assets/remote",
        json={"kind": "image", "name": "Unsafe", "url": "http://127.0.0.1/private.png"},
    )
    assert response.status_code == 422
    assert "private" in response.text.lower()


async def test_cross_origin_mutation_is_rejected(make_client) -> None:
    client = make_client(no_upstream)
    response = await client.post(
        "/api/assets/text",
        headers={"Origin": "https://attacker.example"},
        json={"name": "Blocked", "text": "Should not be stored"},
    )
    assert response.status_code == 403


async def test_dns_rebinding_host_is_rejected(make_client) -> None:
    client = make_client(no_upstream)
    response = await client.get("/api/health", headers={"Host": "attacker.example"})
    assert response.status_code == 400


async def test_alternate_loopback_origin_and_port_are_rejected(make_client) -> None:
    client = make_client(no_upstream)
    alias = await client.get("/api/health", headers={"Host": "localhost:8000"})
    wrong_port = await client.get("/api/health", headers={"Host": "127.0.0.1:8001"})
    assert alias.status_code == 400
    assert wrong_port.status_code == 400


async def test_non_loopback_client_is_rejected_even_with_spoofed_host(make_client) -> None:
    client = make_client(no_upstream, client_address=("203.0.113.10", 4321))
    response = await client.get("/api/health", headers={"Host": "127.0.0.1"})
    assert response.status_code == 403
