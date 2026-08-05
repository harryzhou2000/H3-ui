from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class _ElementIndex(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.nodes: list[tuple[str, dict[str, str | None]]] = []
        self.form_stack: list[str | None] = []
        self.form_buttons: dict[str, list[dict[str, str | None]]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.nodes.append((tag, attributes))
        if tag == "form":
            self.form_stack.append(attributes.get("id"))
        elif tag == "button" and self.form_stack and self.form_stack[-1]:
            self.form_buttons.setdefault(self.form_stack[-1] or "", []).append(attributes)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.form_stack.pop()


def _index() -> _ElementIndex:
    index = _ElementIndex()
    index.feed((ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8"))
    return index


def test_critical_dialogs_have_accessible_names_and_descriptions() -> None:
    index = _index()
    by_id = {attrs["id"]: (tag, attrs) for tag, attrs in index.nodes if attrs.get("id")}

    assert by_id["text-dialog"][1]["aria-labelledby"] == "text-dialog-title"
    assert by_id["remote-dialog"][1]["aria-labelledby"] == "remote-dialog-title"
    assert by_id["remote-dialog"][1]["aria-describedby"] == "remote-dialog-description"
    assert by_id["confirm-dialog"][1]["aria-labelledby"] == "confirm-title"
    assert by_id["confirm-dialog"][1]["aria-describedby"] == "confirm-description"
    assert by_id["rename-dialog"][1]["aria-labelledby"] == "rename-dialog-title"
    for referenced_id in (
        "text-dialog-title",
        "remote-dialog-title",
        "remote-dialog-description",
        "confirm-title",
        "confirm-description",
        "rename-dialog-title",
    ):
        assert referenced_id in by_id


def test_filter_state_and_polling_state_are_exposed_accessibly() -> None:
    index = _index()
    by_id = {attrs["id"]: (tag, attrs) for tag, attrs in index.nodes if attrs.get("id")}
    assert by_id["asset-filters"][1]["role"] == "group"
    assert by_id["job-filters"][1]["role"] == "group"

    filter_buttons = [
        attrs
        for tag, attrs in index.nodes
        if tag == "button" and (attrs.get("data-kind") or attrs.get("data-status"))
    ]
    assert filter_buttons
    assert all(button.get("aria-pressed") in {"true", "false"} for button in filter_buttons)
    assert by_id["polling-status"][1]["aria-live"] == "polite"

    browser_source = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'item.setAttribute("aria-pressed", String(selected))' in browser_source
    assert 'button.setAttribute("aria-pressed", String(selected))' in browser_source
    assert '"Polling paused"' in browser_source


def test_keyboard_focus_and_reduced_motion_have_visible_styles() -> None:
    styles = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")
    assert "button:focus-visible" in styles
    assert "textarea:focus-visible" in styles
    assert "[tabindex]:focus-visible" in styles
    assert ".polling-row.is-paused .live-pulse" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles


def test_context_ir_submission_reveals_the_active_pool() -> None:
    browser_source = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    context_ir = browser_source.split("async function createContextIR()", 1)[1].split(
        "async function uploadFiles", 1
    )[0]
    assert 'setActiveJobFilter("active")' in context_ir


def test_required_dialogs_can_always_be_cancelled_without_validation() -> None:
    index = _index()
    for form_id in ("text-form", "remote-form", "rename-form"):
        cancel_buttons = [
            button for button in index.form_buttons[form_id] if button.get("value") == "cancel"
        ]
        assert len(cancel_buttons) == 2
        assert all(button.get("type") == "submit" for button in cancel_buttons)
        assert all("formnovalidate" in button for button in cancel_buttons)


def test_source_shelf_owns_a_bounded_scroll_region_on_desktop() -> None:
    styles = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")
    library_rule = styles.split(".library-panel {", 1)[1].split("}", 1)[0]
    asset_rule = styles.split(".asset-list {", 1)[1].split("}", 1)[0]

    assert "display: flex" in library_rule
    assert "height: calc(100dvh - 104px)" in library_rule
    assert "min-height: 680px" in library_rule
    assert "position: relative" in library_rule
    assert "overflow: hidden" in library_rule
    assert "min-height: 0" in asset_rule
    assert "overflow-y: auto" in asset_rule
    assert "overscroll-behavior: contain" in asset_rule


def test_attached_images_have_thumbnails_and_all_assets_have_edit_controls() -> None:
    browser_source = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    assert 'thumb.className = "attached-thumb"' in browser_source
    assert "imagePreviewUrl(asset)" in browser_source
    assert 'image.referrerPolicy = "no-referrer"' in browser_source
    assert 'makeButton("Edit"' in browser_source
    assert 'method: "PATCH"' in browser_source


def test_asset_editor_is_type_specific_and_resizes_local_images_as_copies() -> None:
    index = _index()
    by_id = {attrs["id"]: (tag, attrs) for tag, attrs in index.nodes if attrs.get("id")}
    browser_source = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")

    assert by_id["edit-image-preview-row"][0] == "section"
    assert by_id["edit-image-preview"][0] == "img"
    assert by_id["edit-image-resize-row"][0] == "section"
    assert '$("#rename-dialog-title").textContent = `Edit ${asset.kind}`' in browser_source
    assert 'asset.kind === "image" && asset.source_type === "local"' in browser_source
    assert "imagePreviewRow.hidden = !previewUrl" in browser_source
    assert "/resize`" in browser_source
    assert 'value="resize"' in (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert "The original stays unchanged" in (ROOT / "app" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert ".asset-editor-preview" in styles
    assert ".asset-editor-resize[hidden]" in styles


def test_frame_ratio_ui_offers_local_center_crop_without_disabling_ratio() -> None:
    browser_source = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    assert "ratio.disabled = false" in browser_source
    assert "locally crops and downsizes oversized frames without stretching" in browser_source


def test_text_fields_preserve_native_clipboard_and_offer_prompt_buttons() -> None:
    browser_source = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")
    index = _index()
    by_id = {attrs["id"]: (tag, attrs) for tag, attrs in index.nodes if attrs.get("id")}

    assert "setupNativeClipboard" in browser_source
    assert '["copy", "cut"]' in browser_source
    assert 'event.clipboardData?.getData("text/plain")' in browser_source
    assert "field.setRangeText" in browser_source
    assert 'field.dispatchEvent(new Event("input"' in browser_source
    assert "navigator.clipboard?.writeText" in browser_source
    assert "navigator.clipboard?.readText" in browser_source
    assert "user-select: text" in styles
    assert by_id["copy-prompt"][0] == "button"
    assert by_id["paste-prompt"][0] == "button"


def test_saved_requests_can_be_loaded_as_fresh_workspace_intents() -> None:
    browser_source = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    loader = browser_source.split("function loadRequestIntoWorkspace(job)", 1)[1].split(
        "function jobTask", 1
    )[0]

    assert 'makeButton("Load workspace"' in browser_source
    assert "job.request?.content" in browser_source
    assert '$("#prompt").value = promptItem.text' in loader
    assert "state.attached = attached" in loader
    assert "invalidatePreview()" in loader
    assert "loaded as a new workspace intent" in loader


def test_errors_use_a_frontmost_modal_top_layer() -> None:
    index = _index()
    by_id = {attrs["id"]: (tag, attrs) for tag, attrs in index.nodes if attrs.get("id")}
    browser_source = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    toast_source = browser_source.split("function toast(message, isError = false)", 1)[1].split(
        "function setBusy", 1
    )[0]

    assert by_id["error-dialog"][0] == "dialog"
    assert by_id["error-dialog"][1]["aria-describedby"] == "error-dialog-message"
    assert "dialog.showModal()" in toast_source
    assert '$("#error-dialog-message").textContent = message' in toast_source


def test_input_and_output_videos_open_in_an_in_app_preview() -> None:
    index = _index()
    by_id = {attrs["id"]: (tag, attrs) for tag, attrs in index.nodes if attrs.get("id")}
    browser_source = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    assert by_id["video-preview-dialog"][0] == "dialog"
    assert by_id["video-preview-player"][0] == "video"
    assert "function videoPreviewUrl(asset)" in browser_source
    assert "function openVideoPreview(url" in browser_source
    assert "function previewOutput(job)" in browser_source
    assert '$("#video-preview-dialog")' in browser_source
    assert "player.src = url" in browser_source
    assert "window.open(" not in browser_source
    assert 'makeButton("Preview video"' in browser_source
    assert "Creating a local preview copy" in browser_source


def test_completed_ir_text_can_be_viewed_copied_and_used_as_direction() -> None:
    index = _index()
    by_id = {attrs["id"]: (tag, attrs) for tag, attrs in index.nodes if attrs.get("id")}
    browser_source = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    assert by_id["ir-result-dialog"][0] == "dialog"
    assert by_id["ir-result-text"][0] == "textarea"
    assert "function irResultText(job)" in browser_source
    assert '"View IR text"' in browser_source
    assert "openIrResult(job)" in browser_source
    assert "navigator.clipboard.writeText(field.value)" in browser_source
    assert '$("#prompt").value = text' in browser_source
