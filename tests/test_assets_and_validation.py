from __future__ import annotations

import httpx

from tests.conftest import generation_request


def no_upstream(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"Unexpected upstream request: {request.method} {request.url}")


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


async def test_frame_and_reference_modes_cannot_mix(make_client) -> None:
    client = make_client(no_upstream)
    first = (await client.post(
        "/api/assets/remote",
        json={"kind": "image", "name": "Frame", "url": "https://cdn.test/a.png"},
    )).json()
    video = (await client.post(
        "/api/assets/remote",
        json={"kind": "video", "name": "Motion", "url": "https://cdn.test/a.mp4"},
    )).json()
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
    audio = (await client.post(
        "/api/assets/remote",
        json={"kind": "audio", "name": "Voice", "url": "https://cdn.test/voice.mp3"},
    )).json()
    body = generation_request()
    body["ratio"] = "adaptive"
    body["content"].append(
        {"type": "audio", "asset_id": audio["id"], "role": "reference_audio"}
    )
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
