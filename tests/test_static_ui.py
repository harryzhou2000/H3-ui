from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class _ElementIndex(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.nodes: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.nodes.append((tag, dict(attrs)))


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
    for referenced_id in {
        "text-dialog-title",
        "remote-dialog-title",
        "remote-dialog-description",
        "confirm-title",
        "confirm-description",
    }:
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
