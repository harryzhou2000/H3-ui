import {
  ACTIVE_STATUSES,
  createAttemptLedger,
  pollActiveTaskPool,
  taskPresentationChanged,
} from "./pool.mjs";
import { generationCharges, regenerationCharges } from "./billing.mjs";
import { attachmentLabel, reorderAttachedItems } from "./attachments.mjs";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const TERMINAL_STATUSES = new Set([
  "succeeded", "failed", "cancelled", "deleted", "unavailable",
]);
const attempts = createAttemptLedger();

const state = {
  assets: [],
  jobs: [],
  attached: [],
  assetKind: "all",
  jobFilter: "active",
  validatedSignature: null,
  lastPreview: null,
  submitting: false,
  polling: true,
  pollInFlight: new Set(),
  pollCycle: { running: false },
  nextPollAt: new Map(),
  frameMode: false,
  renamingAssetId: null,
};

class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

function detailMessage(detail) {
  if (!detail) return "Unexpected error";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg || String(item)).join(" · ");
  if (detail.message) return detail.message;
  if (detail.detail) return detailMessage(detail.detail);
  return "Unexpected error";
}

async function api(path, options = {}) {
  const request = { ...options };
  request.headers = { Accept: "application/json", ...(options.headers || {}) };
  if (request.body && !(request.body instanceof FormData) && typeof request.body !== "string") {
    request.headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(request.body);
  }
  const response = await fetch(path, request);
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const detail = body?.detail ?? body;
    throw new ApiError(detailMessage(detail), response.status, detail);
  }
  return body;
}

function toast(message, isError = false) {
  if (isError) {
    const dialog = $("#error-dialog");
    $("#error-dialog-message").textContent = message;
    if (dialog.open) dialog.close();
    dialog.showModal();
    return;
  }
  const item = document.createElement("div");
  item.className = "toast";
  item.textContent = message;
  $("#toast-region").append(item);
  window.setTimeout(() => item.remove(), 4800);
}

function setBusy(button, busy, label = "Working…") {
  if (!button.dataset.idleHtml) button.dataset.idleHtml = button.innerHTML;
  button.disabled = busy;
  if (busy) button.textContent = label;
  else button.innerHTML = button.dataset.idleHtml;
}

function requireDurableAttemptLedger() {
  if (attempts.isDurable()) return true;
  toast(
    "Billable task creation is disabled because this browser cannot safely preserve retry IDs. Enable session storage and reload H3 Studio.",
    true,
  );
  return false;
}

function formatBytes(value) {
  if (value == null) return "remote";
  if (value < 1000) return `${value} B`;
  if (value < 1_000_000) return `${(value / 1000).toFixed(1)} KB`;
  return `${(value / 1_000_000).toFixed(1)} MB`;
}

function formatTime(timestamp) {
  if (!timestamp) return "—";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(timestamp * 1000));
}

function kindLabel(kind) {
  return { text: "TXT", image: "IMG", video: "VID", audio: "AUD" }[kind] || "FILE";
}

function imagePreviewUrl(asset) {
  if (asset.preview_url) return asset.preview_url;
  if (
    asset.kind === "image"
    && asset.source_type === "remote"
    && asset.source_url?.startsWith("https://")
  ) return asset.source_url;
  return null;
}

function videoPreviewUrl(asset) {
  if (asset.kind !== "video") return null;
  if (asset.preview_url) return asset.preview_url;
  if (asset.source_type === "remote" && /^https?:\/\//.test(asset.source_url || "")) {
    return asset.source_url;
  }
  return null;
}

function makeButton(label, className, onClick, disabled = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.disabled = disabled;
  if (onClick) button.addEventListener("click", onClick);
  return button;
}

function resetVideoPreview() {
  const player = $("#video-preview-player");
  player.pause();
  player.removeAttribute("src");
  player.load();
  player.hidden = true;
}

function prepareVideoPreview(title, message) {
  const dialog = $("#video-preview-dialog");
  resetVideoPreview();
  $("#video-preview-title").textContent = title;
  const status = $("#video-preview-status");
  status.textContent = message;
  status.hidden = false;
  if (!dialog.open) dialog.showModal();
}

function openVideoPreview(url, title = "Video preview") {
  prepareVideoPreview(title, "Loading video preview…");
  const player = $("#video-preview-player");
  player.removeAttribute("muted");
  player.defaultMuted = false;
  player.muted = false;
  player.volume = 1;
  player.src = url;
  player.hidden = false;
  $("#video-preview-status").hidden = true;
  player.load();
}

function setupNativeClipboard() {
  for (const field of $$('input:not([type="file"]):not([type="checkbox"]), textarea')) {
    for (const eventName of ["copy", "cut"]) {
      field.addEventListener(eventName, (event) => event.stopPropagation());
    }
    if (field.readOnly) continue;
    field.addEventListener("paste", (event) => {
      event.stopPropagation();
      const clipboardText = event.clipboardData?.getData("text/plain");
      if (clipboardText == null) return;
      event.preventDefault();
      const start = field.selectionStart ?? field.value.length;
      const end = field.selectionEnd ?? start;
      const available = field.maxLength > 0
        ? Math.max(0, field.maxLength - (field.value.length - (end - start)))
        : clipboardText.length;
      field.setRangeText(clipboardText.slice(0, available), start, end, "end");
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }
}

async function copyPrompt() {
  const prompt = $("#prompt");
  const selected = prompt.value.slice(prompt.selectionStart, prompt.selectionEnd);
  const text = selected || prompt.value;
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard API unavailable");
    await navigator.clipboard.writeText(text);
    toast(selected ? "Selected prompt text copied." : "Prompt copied.");
  } catch (error) {
    prompt.focus();
    prompt.select();
    const copied = document.execCommand?.("copy");
    toast(copied ? "Prompt copied." : "Use Ctrl/Cmd+C to copy the selected prompt.", !copied);
  }
}

async function pastePrompt() {
  const prompt = $("#prompt");
  try {
    if (!navigator.clipboard?.readText) throw new Error("Clipboard API unavailable");
    const text = await navigator.clipboard.readText();
    prompt.setRangeText(text, prompt.selectionStart, prompt.selectionEnd, "end");
    prompt.dispatchEvent(new Event("input", { bubbles: true }));
    prompt.focus();
  } catch (error) {
    prompt.focus();
    toast("Clipboard access was unavailable. Use Ctrl/Cmd+V in the focused prompt field.", true);
  }
}

async function loadHealth() {
  const pill = $("#connection-pill");
  try {
    const health = await api("/api/health");
    pill.className = `connection-pill ${health.key_configured ? "is-ready" : "is-error"}`;
    pill.innerHTML = "";
    const dot = document.createElement("span");
    dot.className = "status-dot";
    const copy = document.createElement("span");
    copy.className = "status-copy";
    copy.textContent = health.key_configured ? "Server key configured" : "Key not configured";
    pill.append(dot, copy);
  } catch (error) {
    pill.className = "connection-pill is-error";
    pill.textContent = "Server unavailable";
  }
}

async function testConnection() {
  const button = $("#test-connection");
  setBusy(button, true, "Testing…");
  try {
    await api("/api/connection/test", { method: "POST" });
    toast("MiniMax authenticated. Read-only task-list probe succeeded.");
    const pill = $("#connection-pill");
    pill.className = "connection-pill is-ready";
    pill.innerHTML = '<span class="status-dot"></span><span class="status-copy">Connection verified</span>';
  } catch (error) {
    toast(`Connection failed: ${error.message}`, true);
  } finally {
    setBusy(button, false);
  }
}

async function loadAssets() {
  state.assets = await api("/api/assets");
  renderAssets();
  renderAttached();
}

function openRenameDialog(asset) {
  state.renamingAssetId = asset.id;
  const form = $("#rename-form");
  form.reset();
  const input = $("input[name='name']", form);
  const text = $("textarea[name='text']", form);
  const url = $("input[name='url']", form);
  const textRow = $("#edit-text-row");
  const urlRow = $("#edit-url-row");
  const imagePreviewRow = $("#edit-image-preview-row");
  const imageResizeRow = $("#edit-image-resize-row");
  const imagePreview = $("#edit-image-preview");
  input.value = asset.name;
  $("#rename-dialog-title").textContent = `Edit ${asset.kind}`;
  textRow.hidden = asset.source_type !== "text";
  urlRow.hidden = !["remote", "mm_file"].includes(asset.source_type);
  const previewUrl = asset.kind === "image" ? imagePreviewUrl(asset) : null;
  imagePreviewRow.hidden = !previewUrl;
  imageResizeRow.hidden = !(asset.kind === "image" && asset.source_type === "local");
  imagePreview.removeAttribute("src");
  if (previewUrl) imagePreview.src = previewUrl;
  imagePreview.alt = `Preview of ${asset.name}`;
  text.required = !textRow.hidden;
  url.required = !urlRow.hidden;
  text.value = asset.text || "";
  url.value = asset.source_url || "";
  $("textarea[name='notes']", form).value = asset.notes || "";
  $("input[name='tags']", form).value = (asset.tags || []).join(", ");
  $("#rename-dialog").showModal();
  input.focus();
  input.select();
}

function renderAssets() {
  const list = $("#asset-list");
  const search = $("#asset-search").value.trim().toLowerCase();
  const assets = state.assets.filter((asset) => {
    const kindMatch = state.assetKind === "all" || asset.kind === state.assetKind;
    const haystack = `${asset.name} ${asset.notes || ""} ${(asset.tags || []).join(" ")}`.toLowerCase();
    return kindMatch && (!search || haystack.includes(search));
  });
  $("#asset-count").textContent = state.assets.length;
  list.replaceChildren();
  if (!assets.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = state.assets.length
      ? "No inputs match this filter."
      : "Your source shelf is empty. Add a prompt, URL, image, video, or audio file.";
    list.append(empty);
    return;
  }

  for (const asset of assets) {
    const providerReady = Boolean(
      asset.provider_file_id
      && (!asset.provider_expires_at || asset.provider_expires_at > Math.floor(Date.now() / 1000) + 60),
    );
    const card = document.createElement("article");
    card.className = "asset-card";
    const thumb = document.createElement("div");
    thumb.className = "asset-thumb";
    const imageUrl = imagePreviewUrl(asset);
    if (asset.kind === "image" && imageUrl) {
      const image = document.createElement("img");
      image.src = imageUrl;
      image.alt = "";
      image.loading = "lazy";
      image.referrerPolicy = "no-referrer";
      thumb.append(image);
    } else if (asset.kind === "video" && asset.preview_url) {
      const video = document.createElement("video");
      video.src = asset.preview_url;
      video.muted = true;
      video.preload = "metadata";
      thumb.append(video);
    } else {
      thumb.textContent = kindLabel(asset.kind);
    }

    const info = document.createElement("div");
    info.className = "asset-info";
    const name = document.createElement("strong");
    name.className = "asset-name";
    name.textContent = asset.name;
    name.title = asset.name;
    const meta = document.createElement("div");
    meta.className = "asset-meta";
    meta.textContent = `${asset.kind} · ${formatBytes(asset.size)}${providerReady ? " · MiniMax ready" : ""}`;
    const actions = document.createElement("div");
    actions.className = "asset-actions";
    actions.append(
      makeButton(asset.kind === "text" ? "Use prompt" : "Attach", "micro-button", () => attachAsset(asset.id))
    );
    actions.append(makeButton("Edit", "micro-button", () => openRenameDialog(asset)));
    const previewUrl = videoPreviewUrl(asset);
    if (previewUrl) {
      actions.append(
        makeButton("Preview video", "micro-button", () => openVideoPreview(previewUrl, asset.name)),
      );
    }
    if (asset.source_type === "local") {
      actions.append(
        makeButton(
          providerReady ? "Uploaded (7-day)" : asset.provider_file_id ? "Re-upload expired" : "Upload to MiniMax",
          "micro-button",
          () => publishAsset(asset),
          providerReady,
        ),
      );
    }
    actions.append(
      makeButton("Delete local", "micro-button is-danger", () => deleteAsset(asset)),
    );
    info.append(name, meta, actions);
    card.append(thumb, info);
    list.append(card);
  }
}

function defaultRole(asset) {
  if (asset.kind === "video") return "reference_video";
  if (asset.kind === "audio") return "reference_audio";
  const roles = state.attached.map((item) => item.role);
  if (roles.some((role) => role?.startsWith("reference_"))) return "reference_image";
  if (roles.includes("first_frame") && !roles.includes("last_frame")) return "last_frame";
  return roles.length ? "reference_image" : "first_frame";
}

function attachAsset(assetId) {
  const asset = state.assets.find((item) => item.id === assetId);
  if (!asset) return;
  if (asset.kind === "text") {
    $("#prompt").value = asset.text || "";
    updatePromptCount();
    invalidatePreview();
    toast(`Prompt loaded from “${asset.name}”.`);
    return;
  }
  if (state.attached.some((item) => item.assetId === assetId)) {
    toast("That input is already attached.", true);
    return;
  }
  state.attached.push({ assetId, kind: asset.kind, role: defaultRole(asset) });
  enforceRatioMode();
  renderAttached();
  invalidatePreview();
}

function roleOptions(kind) {
  if (kind === "image") {
    return [
      ["first_frame", "First frame"],
      ["last_frame", "Last frame"],
      ["reference_image", "Reference image"],
    ];
  }
  if (kind === "video") return [["reference_video", "Reference video"]];
  return [["reference_audio", "Reference audio"]];
}

function moveAttachedItem(fromIndex, toIndex, { announce = false } = {}) {
  const reordered = reorderAttachedItems(state.attached, fromIndex, toIndex);
  if (reordered === state.attached) return;
  state.attached = reordered;
  renderAttached();
  invalidatePreview();
  if (announce) toast(`Moved ${attachmentLabel(state.attached, toIndex)}.`);
}

function renderAttached() {
  const list = $("#attached-list");
  list.replaceChildren();
  $("#attached-count").textContent = state.attached.length;
  if (!state.attached.length) {
    const empty = document.createElement("div");
    empty.className = "attached-empty";
    empty.textContent = "Attach media from the source shelf, or keep this as text-to-video.";
    list.append(empty);
    updateMode();
    return;
  }
  for (const [attachedIndex, attached] of state.attached.entries()) {
    const asset = state.assets.find((item) => item.id === attached.assetId);
    if (!asset) continue;
    const referenceLabel = attachmentLabel(state.attached, attachedIndex);
    const row = document.createElement("div");
    row.className = "attached-item";
    row.dataset.assetId = attached.assetId;
    const dragHandle = makeButton("⋮⋮", "drag-handle");
    dragHandle.draggable = true;
    dragHandle.title = `Drag to reorder ${referenceLabel}`;
    dragHandle.setAttribute(
      "aria-label",
      `Reorder ${referenceLabel}. Drag, or use the Up and Down arrow keys.`,
    );
    dragHandle.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
      event.preventDefault();
      const offset = event.key === "ArrowUp" ? -1 : 1;
      moveAttachedItem(attachedIndex, attachedIndex + offset, { announce: true });
      const moved = state.attached[attachedIndex + offset];
      if (moved) {
        $(`.attached-item[data-asset-id="${CSS.escape(moved.assetId)}"] .drag-handle`)?.focus();
      }
    });
    dragHandle.addEventListener("dragstart", (event) => {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", attached.assetId);
      row.classList.add("is-dragging");
    });
    dragHandle.addEventListener("dragend", () => {
      for (const item of $$(".attached-item")) {
        item.classList.remove("is-dragging", "drop-before", "drop-after");
      }
    });
    row.addEventListener("dragover", (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      const after = event.clientY >= row.getBoundingClientRect().top + row.offsetHeight / 2;
      row.classList.toggle("drop-before", !after);
      row.classList.toggle("drop-after", after);
    });
    row.addEventListener("dragleave", () => {
      row.classList.remove("drop-before", "drop-after");
    });
    row.addEventListener("drop", (event) => {
      event.preventDefault();
      const draggedAssetId = event.dataTransfer.getData("text/plain");
      const fromIndex = state.attached.findIndex((item) => item.assetId === draggedAssetId);
      const targetIndex = state.attached.findIndex((item) => item === attached);
      const after = event.clientY >= row.getBoundingClientRect().top + row.offsetHeight / 2;
      let toIndex = targetIndex + (after ? 1 : 0);
      if (fromIndex < toIndex) toIndex -= 1;
      toIndex = Math.min(Math.max(toIndex, 0), state.attached.length - 1);
      moveAttachedItem(fromIndex, toIndex);
    });
    const thumb = document.createElement("div");
    thumb.className = "attached-thumb";
    const imageUrl = imagePreviewUrl(asset);
    if (asset.kind === "image" && imageUrl) {
      const image = document.createElement("img");
      image.src = imageUrl;
      image.alt = "";
      image.loading = "lazy";
      image.referrerPolicy = "no-referrer";
      thumb.append(image);
    } else {
      thumb.textContent = kindLabel(asset.kind);
    }
    const info = document.createElement("div");
    info.className = "attached-info";
    const label = document.createElement("span");
    label.className = "media-reference-label";
    label.textContent = referenceLabel;
    const name = document.createElement("strong");
    name.textContent = asset.name;
    name.title = asset.name;
    info.append(label, name);
    const select = document.createElement("select");
    select.setAttribute("aria-label", `Role for ${asset.name}`);
    for (const [value, label] of roleOptions(attached.kind)) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      option.selected = value === attached.role;
      select.append(option);
    }
    select.addEventListener("change", () => {
      attached.role = select.value;
      enforceRatioMode();
      renderAttached();
      invalidatePreview();
    });
    const remove = makeButton("×", "remove-attached", () => {
      state.attached = state.attached.filter((item) => item !== attached);
      enforceRatioMode();
      renderAttached();
      invalidatePreview();
    });
    remove.setAttribute("aria-label", `Remove ${asset.name}`);
    row.append(dragHandle, thumb, info, select, remove);
    list.append(row);
  }
  updateMode();
}

function updateMode() {
  const roles = state.attached.map((item) => item.role);
  const hasFrames = roles.some((role) => role === "first_frame" || role === "last_frame");
  const hasReferences = roles.some((role) => role?.startsWith("reference_"));
  const chip = $("#mode-chip");
  if (hasFrames && hasReferences) {
    chip.textContent = "Resolve mode conflict";
    chip.style.background = "var(--coral-dark)";
  } else if (hasFrames) {
    chip.textContent = roles.includes("last_frame") ? "First / last frame" : "Image to video";
    chip.style.background = "var(--green)";
  } else if (hasReferences) {
    chip.textContent = "Multimodal reference";
    chip.style.background = "var(--green)";
  } else {
    chip.textContent = "Text to video";
    chip.style.background = "var(--green)";
  }
}

function enforceRatioMode() {
  const ratio = $("#ratio");
  const frameMode = state.attached.some(
    (item) => item.role === "first_frame" || item.role === "last_frame",
  );
  if (frameMode && !state.frameMode) ratio.value = "adaptive";
  if (!frameMode && state.frameMode && ratio.value === "adaptive") ratio.value = "16:9";
  state.frameMode = frameMode;
  ratio.disabled = false;
  $("#ratio-help").textContent = frameMode
    ? "Adaptive keeps the original frame. A concrete ratio locally crops and downsizes oversized frames without stretching."
    : "Choose the requested output frame.";
}

function updatePromptCount() {
  $("#prompt-count").textContent = `${$("#prompt").value.length.toLocaleString()} / 7,000`;
}

function composerSignature() {
  return JSON.stringify({
    prompt: $("#prompt").value.trim(),
    attached: state.attached.map(({ assetId, kind, role }) => ({ assetId, kind, role })),
    resolution: $("#resolution").value,
    duration: Number($("#duration").value),
    ratio: $("#ratio").value,
    watermark: $("#watermark").checked,
  });
}

function buildContent() {
  const content = [{ type: "text", text: $("#prompt").value.trim() }];
  for (const item of state.attached) {
    content.push({ type: item.kind, asset_id: item.assetId, role: item.role });
  }
  return content;
}

function buildGeneration(confirmed = false, clientRequestId = attempts.generation()) {
  return {
    client_request_id: clientRequestId,
    model: "MiniMax-H3",
    content: buildContent(),
    resolution: $("#resolution").value,
    duration: Number($("#duration").value),
    ratio: $("#ratio").value,
    aigc_watermark: $("#watermark").checked,
    confirmed,
  };
}

function buildContextIR(confirmed = false, clientRequestId = attempts.context()) {
  const generation = buildGeneration(confirmed, clientRequestId);
  delete generation.resolution;
  delete generation.aigc_watermark;
  return generation;
}

function invalidatePreview({ rotateAttempt = true } = {}) {
  if (rotateAttempt) attempts.rotateScene();
  state.validatedSignature = null;
  state.lastPreview = null;
  $("#generate").disabled = true;
  updateCost();
  const panel = $("#validation-panel");
  panel.className = "validation-panel is-neutral";
  $(".validation-icon", panel).textContent = "◇";
  $("strong", panel).textContent = "Request changed — review again";
  $("p", panel).textContent = "Review validates the exact payload locally. It does not contact MiniMax.";
}

function showValidation(valid, title, message) {
  const panel = $("#validation-panel");
  panel.className = `validation-panel ${valid ? "is-valid" : "is-error"}`;
  $(".validation-icon", panel).textContent = valid ? "✓" : "!";
  $("strong", panel).textContent = title;
  $("p", panel).textContent = message;
}

async function reviewRequest() {
  const button = $("#review-request");
  setBusy(button, true, "Reviewing…");
  try {
    const preview = await api("/api/jobs/preview", {
      method: "POST",
      body: buildGeneration(false),
    });
    state.validatedSignature = composerSignature();
    state.lastPreview = preview;
    $("#request-json").textContent = JSON.stringify(preview.payload, null, 2);
    $("#generate").disabled = !attempts.isDurable();
    if (attempts.isDurable()) {
      showValidation(
        true,
        "Request is valid",
        `${formatBytes(preview.estimated_request_bytes)} JSON payload. Generation is still blocked until explicit confirmation.`,
      );
    } else {
      showValidation(
        false,
        "Billable actions are safely disabled",
        "Enable browser session storage and reload so retry IDs can survive a lost response.",
      );
    }
  } catch (error) {
    state.validatedSignature = null;
    $("#generate").disabled = true;
    $("#request-json").textContent = `Validation failed: ${error.message}`;
    showValidation(false, "Fix this scene before submitting", error.message);
  } finally {
    setBusy(button, false);
  }
}

function mediaCounts(items = state.attached) {
  return items.reduce((counts, item) => {
    const kind = item.kind || item.type;
    if (kind === "image") counts.imageCount += 1;
    if (kind === "video") counts.videoCount += 1;
    return counts;
  }, { imageCount: 0, videoCount: 0 });
}

function currentGenerationCharges() {
  return generationCharges({
    resolution: $("#resolution").value,
    duration: Number($("#duration").value),
    ...mediaCounts(),
  });
}

function updateCost() {
  const charges = currentGenerationCharges();
  $("#cost-inline").textContent = charges.videoCount
    ? `known ¥${charges.knownCost.toFixed(2)} + input video`
    : `¥${charges.knownCost.toFixed(2)}`;
}

function confirmOperation({ title, description, summary = [], label, accent = "Confirm" }) {
  const dialog = $("#confirm-dialog");
  const check = $("#confirm-check");
  const submit = $("#confirm-submit");
  $("#confirm-title").textContent = title;
  $("#confirm-description").textContent = description;
  $("#confirm-check-label").textContent = label;
  submit.textContent = accent;
  check.checked = false;
  submit.disabled = true;
  dialog.returnValue = "cancel";
  const summaryNode = $("#confirm-summary");
  summaryNode.replaceChildren();
  for (const item of summary) {
    const cell = document.createElement("div");
    const value = document.createElement("strong");
    value.textContent = item.value;
    const key = document.createElement("small");
    key.textContent = item.label;
    cell.append(value, key);
    summaryNode.append(cell);
  }
  return new Promise((resolve) => {
    const onChange = () => { submit.disabled = !check.checked; };
    const onCancel = (event) => {
      event.preventDefault();
      dialog.close("cancel");
    };
    const onClose = () => {
      check.removeEventListener("change", onChange);
      dialog.removeEventListener("cancel", onCancel);
      dialog.removeEventListener("close", onClose);
      resolve(dialog.returnValue === "default" && check.checked);
    };
    check.addEventListener("change", onChange);
    dialog.addEventListener("cancel", onCancel);
    dialog.addEventListener("close", onClose);
    dialog.showModal();
  });
}

async function generateVideo() {
  if (state.submitting) return;
  if (!requireDurableAttemptLedger()) return;
  if (state.validatedSignature !== composerSignature()) {
    toast("Review the current request before generating.", true);
    return;
  }
  const duration = Number($("#duration").value);
  const resolution = $("#resolution").value;
  const charges = currentGenerationCharges();
  const confirmed = await confirmOperation({
    title: "Add this job to the pool?",
    description: `This creates a new MiniMax task; existing tasks continue. Published input pricing is added to the output: the first five images are free, then ¥0.20 each; each input-video second costs ¥${charges.inputVideoRate.toFixed(2)} at ${resolution}. Audio is free. Video durations are not inspected here, so the displayed subtotal may not be final.`,
    summary: [
      { value: `¥${charges.outputCost.toFixed(2)}`, label: `Output · ${duration}s at ¥${charges.outputRate.toFixed(2)}/s` },
      { value: `¥${charges.excessImageCost.toFixed(2)}`, label: `${charges.excessImageCount} images beyond five` },
      { value: charges.videoCount ? `${charges.videoCount} · duration unmeasured` : "None", label: `Input video · ¥${charges.inputVideoRate.toFixed(2)}/s` },
    ],
    label: "I understand the output and input charges and want one additional active task.",
    accent: "Generate & add",
  });
  if (!confirmed) return;

  const button = $("#generate");
  state.submitting = true;
  setBusy(button, true, "Submitting once…");
  try {
    const requestId = attempts.generation();
    if (!requireDurableAttemptLedger()) return;
    const result = await api("/api/jobs", {
      method: "POST",
      body: buildGeneration(true, requestId),
    });
    attempts.markSucceeded("generation");
    toast(`Task ${result.task_id} joined the active pool.`);
    state.jobFilter = "active";
    setActiveJobFilter("active");
    await loadJobs();
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.submitting = false;
    setBusy(button, false);
    button.disabled = !attempts.isDurable()
      || state.validatedSignature !== composerSignature();
  }
}

async function createContextIR() {
  if (!requireDurableAttemptLedger()) return;
  const button = $("#context-ir");
  setBusy(button, true, "Validating…");
  try {
    await api("/api/context-ir/preview", { method: "POST", body: buildContextIR(false) });
    const confirmed = await confirmOperation({
      title: "Create an H3 Context IR task?",
      description: "This token-billed operation creates another asynchronous task in the pool and returns an enhanced text prompt.",
      summary: [
        { value: `${$("#duration").value}s`, label: "Target duration" },
        { value: $("#ratio").value, label: "Ratio" },
        { value: `${state.attached.length}`, label: "Media inputs" },
      ],
      label: "I understand Context IR is token-billed and creates a separate task.",
      accent: "Create IR task",
    });
    if (!confirmed) return;
    const requestId = attempts.context();
    if (!requireDurableAttemptLedger()) return;
    const result = await api("/api/context-ir", {
      method: "POST",
      body: buildContextIR(true, requestId),
    });
    attempts.markSucceeded("context-ir");
    toast(`Context IR task ${result.task_id} joined the pool.`);
    setActiveJobFilter("active");
    await loadJobs();
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(button, false);
    button.disabled = !attempts.isDurable();
  }
}

async function uploadFiles(files) {
  for (const file of files) {
    const form = new FormData();
    form.append("file", file, file.name);
    try {
      await api("/api/assets/upload", { method: "POST", body: form });
      toast(`Added ${file.name} to the local source shelf.`);
    } catch (error) {
      toast(`${file.name}: ${error.message}`, true);
    }
  }
  await loadAssets();
  $("#file-input").value = "";
}

async function publishAsset(asset) {
  const confirmed = await confirmOperation({
    title: "Upload this input to MiniMax?",
    description: "The file will be sent to MiniMax as video_generation_input and kept there for up to seven days. H3 Studio will not auto-delete it because MiniMax’s input-file delete purpose is undocumented.",
    summary: [
      { value: asset.kind, label: "Type" },
      { value: formatBytes(asset.size), label: "Size" },
      { value: "7 days", label: "Provider TTL" },
    ],
    label: "I explicitly authorize sending this local file to MiniMax.",
    accent: "Upload input",
  });
  if (!confirmed) return;
  try {
    await api(`/api/assets/${encodeURIComponent(asset.id)}/publish`, {
      method: "POST",
      body: { confirmed: true },
    });
    toast("Input uploaded. Future requests can use its compact mm_file reference.");
    await loadAssets();
    invalidatePreview({ rotateAttempt: false });
  } catch (error) {
    toast(error.message, true);
  }
}

async function deleteAsset(asset) {
  const confirmed = window.confirm(
    `Delete “${asset.name}” from this local library? This does not delete any MiniMax copy.`,
  );
  if (!confirmed) return;
  try {
    await api(`/api/assets/${encodeURIComponent(asset.id)}`, { method: "DELETE" });
    state.attached = state.attached.filter((item) => item.assetId !== asset.id);
    await loadAssets();
    invalidatePreview();
    toast("Local input deleted. No remote task or file was touched.");
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadJobs() {
  state.jobs = await api("/api/jobs");
  renderJobs();
}

function workspaceRequest(job) {
  if (Array.isArray(job.request?.content)) return job.request;
  if (job.request?.source_task_id) {
    return state.jobs.find((candidate) => candidate.task_id === job.request.source_task_id)?.request;
  }
  return null;
}

function loadRequestIntoWorkspace(job) {
  const request = workspaceRequest(job);
  const content = request?.content;
  if (!Array.isArray(content)) {
    toast("This synced record does not contain a reconstructable local request.", true);
    return;
  }
  const promptItem = content.find((item) => item.type === "text" && item.text);
  if (!promptItem) {
    toast("This request has no reusable text prompt.", true);
    return;
  }

  const missing = [];
  const attached = [];
  for (const item of content.filter((candidate) => candidate.type !== "text")) {
    const asset = state.assets.find((candidate) => candidate.id === item.asset_id);
    if (!asset) {
      missing.push(item.asset_id || item.type);
      continue;
    }
    attached.push({ assetId: asset.id, kind: asset.kind, role: item.role || defaultRole(asset) });
  }

  $("#prompt").value = promptItem.text;
  state.attached = attached;
  state.frameMode = false;
  if (request.resolution && $(`#resolution option[value='${request.resolution}']`)) {
    $("#resolution").value = request.resolution;
  }
  if (request.duration && $(`#duration option[value='${request.duration}']`)) {
    $("#duration").value = String(request.duration);
  }
  $("#watermark").checked = Boolean(request.aigc_watermark);
  enforceRatioMode();
  if (request.ratio && $(`#ratio option[value='${request.ratio}']`)) {
    $("#ratio").value = request.ratio;
  }
  updatePromptCount();
  renderAttached();
  invalidatePreview();
  $(".composer-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  toast(
    missing.length
      ? `Request loaded with ${missing.length} missing source item(s). Reattach them before review.`
      : `Request ${job.task_id} loaded as a new workspace intent.`,
    Boolean(missing.length),
  );
}

function jobTask(job) {
  return job.response?.task || {};
}

function irResultText(job) {
  const task = jobTask(job);
  const candidates = [task.content?.prompt, task.content?.text, task.prompt, task.text];
  return candidates.find((value) => typeof value === "string" && value.trim()) || null;
}

function openIrResult(job) {
  const text = irResultText(job);
  if (!text) {
    toast("The completed IR task did not include returned text. Refresh the task and try again.", true);
    return;
  }
  $("#ir-result-text").value = text;
  $("#ir-result-dialog").showModal();
}

async function copyIrResult() {
  const field = $("#ir-result-text");
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard API unavailable");
    await navigator.clipboard.writeText(field.value);
    toast("IR text copied.");
  } catch (error) {
    field.focus();
    field.select();
    const copied = document.execCommand?.("copy");
    toast(copied ? "IR text copied." : "Use Ctrl/Cmd+C to copy the selected IR text.", !copied);
  }
}

function useIrResultAsDirection() {
  const text = $("#ir-result-text").value;
  if (!text.trim()) return;
  $("#prompt").value = text;
  updatePromptCount();
  invalidatePreview();
  $("#ir-result-dialog").close("use");
  $(".composer-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  toast("IR text loaded into Direction as a fresh workspace intent.");
}

function jobSettings(job) {
  const task = jobTask(job);
  return {
    resolution: task.resolution || job.request?.resolution || "—",
    duration: task.duration || job.request?.duration || "—",
    ratio: task.ratio || job.request?.ratio || "—",
  };
}

function setActiveJobFilter(filter) {
  state.jobFilter = filter;
  $$("#job-filters button").forEach((button) => {
    const selected = button.dataset.status === filter;
    button.classList.toggle("is-active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  renderJobs();
}

function renderJobs() {
  const list = $("#job-list");
  const active = state.jobs.filter((job) => ACTIVE_STATUSES.has(job.status));
  $("#active-count").innerHTML = `<b>${active.length}</b> active`;
  let jobs = state.jobs;
  if (state.jobFilter === "active") jobs = jobs.filter((job) => ACTIVE_STATUSES.has(job.status));
  if (state.jobFilter === "complete") jobs = jobs.filter((job) => TERMINAL_STATUSES.has(job.status));
  list.replaceChildren();
  if (!jobs.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = state.jobFilter === "active"
      ? "No active tasks. Your next submission will join this pool."
      : "No tasks in this view.";
    list.append(empty);
    return;
  }

  for (const job of jobs) {
    const task = jobTask(job);
    const settings = jobSettings(job);
    const card = document.createElement("article");
    card.className = `job-card${ACTIVE_STATUSES.has(job.status) ? " is-active" : ""}`;
    const head = document.createElement("div");
    head.className = "job-card-head";
    const identity = document.createElement("div");
    const id = document.createElement("strong");
    id.className = "job-id";
    id.textContent = job.task_id;
    id.title = job.task_id;
    const kind = document.createElement("span");
    kind.className = "job-kind";
    kind.textContent = job.operation.replaceAll("_", " ");
    identity.append(id, kind);
    const status = document.createElement("span");
    status.className = `status-chip ${job.status}`;
    status.textContent = job.status;
    head.append(identity, status);

    const meta = document.createElement("div");
    meta.className = "job-meta-grid";
    for (const value of [settings.resolution, `${settings.duration}${settings.duration === "—" ? "" : "s"}`, settings.ratio]) {
      const cell = document.createElement("span");
      cell.textContent = value;
      meta.append(cell);
    }
    const time = document.createElement("p");
    time.className = "job-message";
    time.textContent = `Added ${formatTime(job.created_at)}${ACTIVE_STATUSES.has(job.status) ? " · remains in active pool" : ""}`;
    card.append(head, meta, time);

    if (task.error?.message) {
      const error = document.createElement("p");
      error.className = "job-message is-error";
      error.textContent = task.error.message;
      card.append(error);
    }
    if (task.content?.prompt) {
      const prompt = document.createElement("p");
      prompt.className = "job-message";
      prompt.textContent = task.content.prompt.slice(0, 240) + (task.content.prompt.length > 240 ? "…" : "");
      card.append(prompt);
    }

    const actions = document.createElement("div");
    actions.className = "job-actions";
    if (workspaceRequest(job)) {
      actions.append(
        makeButton("Load workspace", "micro-button", () => loadRequestIntoWorkspace(job)),
      );
    }
    actions.append(
      makeButton("Refresh", "micro-button", () => refreshJob(job.task_id)),
    );
    if (job.status === "queued") {
      actions.append(
        makeButton("Kill queued task", "micro-button is-danger", () => remoteTaskAction(job)),
      );
    } else if (job.status === "running") {
      const unavailable = makeButton("Kill unavailable", "micro-button is-danger", null, true);
      unavailable.title = "MiniMax rejects cancellation after a task begins running";
      actions.append(unavailable);
    } else if (job.status === "succeeded") {
      if (job.operation === "h3_context_ir") {
        const viewResult = makeButton(
          irResultText(job) ? "View IR text" : "IR text unavailable",
          "micro-button",
          () => openIrResult(job),
          !irResultText(job),
        );
        if (viewResult.disabled) {
          viewResult.title = "Refresh until MiniMax returns the completed IR prompt";
        }
        actions.append(viewResult);
      }
      if (task.modality !== "text" && job.operation !== "h3_context_ir") {
        actions.append(makeButton("Preview video", "micro-button", () => previewOutput(job)));
        actions.append(makeButton("Save MP4", "micro-button", () => saveOutput(job)));
      }
      if (settings.resolution === "768P" && job.operation === "generation") {
        const sourceDuration = Number(settings.duration);
        const durationKnown = Number.isInteger(sourceDuration)
          && sourceDuration >= 4
          && sourceDuration <= 15;
        const regenerate = makeButton(
          durationKnown ? "Regenerate 2K" : "Duration required",
          "micro-button",
          () => regenerateJob(job),
          !durationKnown || !attempts.isDurable(),
        );
        if (!durationKnown) {
          regenerate.title = "Refresh this task until MiniMax supplies its 4–15 second duration";
        } else if (!attempts.isDurable()) {
          regenerate.title = "Enable browser session storage and reload before billable actions";
        }
        actions.append(regenerate);
      }
      actions.append(
        makeButton("Delete remote record", "micro-button is-danger", () => remoteTaskAction(job)),
      );
    } else if (job.status === "failed") {
      actions.append(
        makeButton("Delete remote record", "micro-button is-danger", () => remoteTaskAction(job)),
      );
    }
    if (!ACTIVE_STATUSES.has(job.status)) {
      actions.append(
        makeButton("Remove local history", "micro-button", () => removeLocalJob(job)),
      );
    }
    card.append(actions);
    list.append(card);
  }
}

async function requestJobRefresh(taskId, quiet = false) {
  try {
    const force = quiet ? "" : "?force=true";
    const result = await api(`/api/jobs/${encodeURIComponent(taskId)}/refresh${force}`, {
      method: "POST",
    });
    const current = state.jobs.find((job) => job.task_id === taskId);
    const changed = taskPresentationChanged(current, result);
    if (!quiet && changed) await loadJobs();
    if (!quiet) toast(`Task ${taskId} refreshed.`);
    return changed;
  } catch (error) {
    if (!quiet) toast(error.message, true);
    const retry = Number(error.detail?.retry_after || error.detail?.detail?.retry_after || 0);
    state.nextPollAt.set(taskId, Date.now() + Math.max(15, retry || 15) * 1000);
    return false;
  }
}

async function refreshJob(taskId, quiet = false) {
  if (state.pollInFlight.has(taskId)) return false;
  state.pollInFlight.add(taskId);
  try {
    return await requestJobRefresh(taskId, quiet);
  } finally {
    state.pollInFlight.delete(taskId);
  }
}

async function pollActiveJobs() {
  const changed = await pollActiveTaskPool({
    jobs: state.jobs,
    enabled: state.polling,
    hidden: document.hidden,
    nextPollAt: state.nextPollAt,
    inFlight: state.pollInFlight,
    cycle: state.pollCycle,
    refresh: (taskId) => requestJobRefresh(taskId, true),
    concurrency: 4,
    maxTasksPerCycle: 8,
    pollIntervalMs: 8000,
  });
  if (changed) await loadJobs();
}

function updatePollingStatus(enabled) {
  const row = $(".polling-row");
  row.classList.toggle("is-paused", !enabled);
  $("#polling-copy").textContent = enabled ? "Polling every 8 seconds" : "Polling paused";
}

async function syncProviderTasks() {
  const button = $("#sync-tasks");
  button.classList.add("is-spinning");
  button.disabled = true;
  try {
    const result = await api("/api/provider/tasks?page_num=1&page_size=100", {
      method: "POST",
    });
    await loadJobs();
    toast(`Synced ${result.items?.length || 0} recent MiniMax tasks into the local pool.`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.classList.remove("is-spinning");
    button.disabled = false;
  }
}

async function remoteTaskAction(job) {
  const isKill = job.status === "queued";
  const confirmed = await confirmOperation({
    title: isKill ? "Kill this queued task?" : "Delete this remote task record?",
    description: isKill
      ? "This is the only state MiniMax allows H3 Studio to cancel. The local history will remain marked cancelled."
      : "This removes the succeeded or failed record from MiniMax. Any MP4 already saved locally remains untouched.",
    summary: [
      { value: job.status, label: "Fresh expected state" },
      { value: job.task_id.slice(-8), label: "Task ID ending" },
      { value: isKill ? "Cancel" : "Delete", label: "Remote action" },
    ],
    label: isKill
      ? "I explicitly want to cancel this queued task."
      : "I explicitly want to delete this MiniMax task record.",
    accent: isKill ? "Kill queued task" : "Delete record",
  });
  if (!confirmed) return;
  try {
    const result = await api(`/api/jobs/${encodeURIComponent(job.task_id)}/remote`, {
      method: "DELETE",
      body: { expected_status: job.status, confirmed: true },
    });
    toast(`Remote task ${result.action || result.status}. Local history was kept.`);
    await loadJobs();
  } catch (error) {
    toast(error.message, true);
    await refreshJob(job.task_id, true);
    await loadJobs();
  }
}

async function removeLocalJob(job) {
  if (!window.confirm(`Remove task ${job.task_id} from local history? No MiniMax request will be made.`)) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(job.task_id)}/local`, { method: "DELETE" });
    await loadJobs();
    toast("Local history removed. Remote state was not changed.");
  } catch (error) {
    toast(error.message, true);
  }
}

async function previewOutput(job) {
  prepareVideoPreview(`Output ${job.task_id}`, "Creating a local preview copy…");
  try {
    let previewUrl;
    if (job.downloaded_filename) {
      previewUrl = `/api/downloads/${encodeURIComponent(job.downloaded_filename)}`;
    } else {
      toast("Creating a local preview copy of the completed video…");
      const result = await api(`/api/jobs/${encodeURIComponent(job.task_id)}/download`, {
        method: "POST",
      });
      previewUrl = result.download_url;
      await loadJobs();
    }
    openVideoPreview(previewUrl, `Output ${job.task_id}`);
  } catch (error) {
    $("#video-preview-dialog").close("error");
    toast(error.message, true);
  }
}

async function saveOutput(job) {
  try {
    toast("Refreshing the expiring result link and saving the MP4 locally…");
    const result = await api(`/api/jobs/${encodeURIComponent(job.task_id)}/download`, {
      method: "POST",
    });
    const link = document.createElement("a");
    link.href = result.download_url;
    link.download = result.filename;
    document.body.append(link);
    link.click();
    link.remove();
    await loadJobs();
    toast(`Saved ${result.filename} (${formatBytes(result.size)}).`);
  } catch (error) {
    toast(error.message, true);
  }
}

async function regenerateJob(job) {
  if (!requireDurableAttemptLedger()) return;
  const task = jobTask(job);
  const duration = Number(task.duration || job.request?.duration);
  if (!Number.isInteger(duration) || duration < 4 || duration > 15) {
    toast("Regeneration is blocked until the source task reports its exact duration.", true);
    return;
  }
  const originalContent = Array.isArray(job.request?.content) ? job.request.content : null;
  const inputCounts = mediaCounts(originalContent || []);
  const charges = regenerationCharges({ duration, ...inputCounts });
  const confirmed = await confirmOperation({
    title: "Add a 2K regeneration task?",
    description: "This creates another task from the eligible 768P source. MiniMax charges ¥0.30 per regenerated output second and re-bills the original task inputs: first five images free, then ¥0.15 each; input video ¥0.30 per second; audio free. Source-task regeneration may require whitelist access.",
    summary: [
      { value: `¥${charges.outputCost.toFixed(2)}`, label: `Output · ${duration}s at ¥0.30/s` },
      { value: originalContent ? `¥${charges.excessImageCost.toFixed(2)}` : "Unknown", label: originalContent ? `${charges.excessImageCount} original images beyond five` : "Original image charges" },
      { value: originalContent ? (charges.videoCount ? `${charges.videoCount} · duration unmeasured` : "None") : "Unknown", label: "Original video · ¥0.30/s" },
    ],
    label: "I understand the regeneration output and re-billed input charges.",
    accent: "Regenerate in 2K",
  });
  if (!confirmed) return;
  try {
    const requestId = attempts.regeneration(job.task_id);
    if (!requireDurableAttemptLedger()) return;
    const result = await api("/api/regenerations", {
      method: "POST",
      body: {
        client_request_id: requestId,
        model: "MiniMax-H3",
        source_task_id: job.task_id,
        resolution: "2K",
        aigc_watermark: false,
        confirmed: true,
      },
    });
    attempts.markSucceeded("regeneration", job.task_id);
    toast(`Regeneration task ${result.task_id} joined the active pool.`);
    setActiveJobFilter("active");
    await loadJobs();
  } catch (error) {
    toast(error.message, true);
  }
}

function setupDialogs() {
  $("#add-text").addEventListener("click", () => $("#text-dialog").showModal());
  $("#add-remote").addEventListener("click", () => $("#remote-dialog").showModal());

  $("#text-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (event.submitter?.value === "cancel") {
      $("#text-dialog").close("cancel");
      return;
    }
    const form = new FormData(event.currentTarget);
    try {
      await api("/api/assets/text", {
        method: "POST",
        body: {
          name: form.get("name"),
          text: form.get("text"),
          tags: String(form.get("tags") || "").split(",").map((tag) => tag.trim()).filter(Boolean),
        },
      });
      event.currentTarget.reset();
      $("#text-dialog").close("default");
      await loadAssets();
      toast("Text prompt saved to the source shelf.");
    } catch (error) {
      toast(error.message, true);
    }
  });

  $("#remote-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (event.submitter?.value === "cancel") {
      $("#remote-dialog").close("cancel");
      return;
    }
    const form = new FormData(event.currentTarget);
    try {
      await api("/api/assets/remote", {
        method: "POST",
        body: {
          kind: form.get("kind"),
          name: form.get("name"),
          url: form.get("url"),
          tags: [],
        },
      });
      event.currentTarget.reset();
      $("#remote-dialog").close("default");
      await loadAssets();
      toast("Remote input added to the source shelf.");
    } catch (error) {
      toast(error.message, true);
    }
  });

  $("#rename-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (event.submitter?.value === "cancel") {
      state.renamingAssetId = null;
      $("#rename-dialog").close("cancel");
      return;
    }
    const assetId = state.renamingAssetId;
    const form = new FormData(event.currentTarget);
    if (!assetId) return;
    const asset = state.assets.find((item) => item.id === assetId);
    if (!asset) return;
    if (event.submitter?.value === "resize") {
      const button = event.submitter;
      const form = new FormData(event.currentTarget);
      setBusy(button, true, "Creating copy…");
      try {
        const resized = await api(`/api/assets/${encodeURIComponent(assetId)}/resize`, {
          method: "POST",
          body: {
            ratio: String(form.get("resize_ratio") || "16:9"),
            max_edge: Number(form.get("resize_max_edge") || 2048),
          },
        });
        state.renamingAssetId = null;
        $("#rename-dialog").close("default");
        await loadAssets();
        toast(`Created ${resized.name}. The original image is unchanged.`);
      } catch (error) {
        toast(error.message, true);
      } finally {
        setBusy(button, false);
      }
      return;
    }
    const body = {
      name: String(form.get("name") || "").trim(),
      notes: String(form.get("notes") || ""),
      tags: String(form.get("tags") || "").split(",").map((tag) => tag.trim()).filter(Boolean),
    };
    if (asset.source_type === "text") body.text = String(form.get("text") || "");
    if (["remote", "mm_file"].includes(asset.source_type)) {
      body.url = String(form.get("url") || "").trim();
    }
    try {
      await api(`/api/assets/${encodeURIComponent(assetId)}`, {
        method: "PATCH",
        body,
      });
      state.renamingAssetId = null;
      $("#rename-dialog").close("default");
      await loadAssets();
      if (state.attached.some((item) => item.assetId === assetId)) invalidatePreview();
      toast("Input updated.");
    } catch (error) {
      toast(error.message, true);
    }
  });
}

function setupEvents() {
  $("#test-connection").addEventListener("click", testConnection);
  $("#file-input").addEventListener("change", (event) => uploadFiles([...event.target.files]));
  $("#asset-search").addEventListener("input", renderAssets);
  $$("#asset-filters button").forEach((button) => {
    button.addEventListener("click", () => {
      state.assetKind = button.dataset.kind;
      $$("#asset-filters button").forEach((item) => {
        const selected = item === button;
        item.classList.toggle("is-active", selected);
        item.setAttribute("aria-pressed", String(selected));
      });
      renderAssets();
    });
  });

  const dropZone = $("#drop-zone");
  for (const eventName of ["dragenter", "dragover"]) {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropZone.classList.add("is-over");
    });
  }
  for (const eventName of ["dragleave", "drop"]) {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropZone.classList.remove("is-over");
    });
  }
  dropZone.addEventListener("drop", (event) => uploadFiles([...event.dataTransfer.files]));
  dropZone.addEventListener("click", () => $("#file-input").click());
  dropZone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      $("#file-input").click();
    }
  });

  $("#prompt").addEventListener("input", () => {
    updatePromptCount();
    invalidatePreview();
  });
  $("#copy-prompt").addEventListener("click", copyPrompt);
  $("#paste-prompt").addEventListener("click", pastePrompt);
  $("#copy-ir-result").addEventListener("click", copyIrResult);
  $("#use-ir-result").addEventListener("click", useIrResultAsDirection);
  $("#video-preview-dialog").addEventListener("close", resetVideoPreview);
  for (const id of ["resolution", "duration", "ratio", "watermark"]) {
    $(`#${id}`).addEventListener("change", () => {
      updateCost();
      invalidatePreview();
    });
  }
  $("#clear-composer").addEventListener("click", () => {
    $("#prompt").value = "";
    state.attached = [];
    updatePromptCount();
    enforceRatioMode();
    renderAttached();
    invalidatePreview();
  });
  $("#review-request").addEventListener("click", reviewRequest);
  $("#generate").addEventListener("click", generateVideo);
  $("#context-ir").addEventListener("click", createContextIR);
  $("#sync-tasks").addEventListener("click", syncProviderTasks);
  $("#polling-toggle").addEventListener("change", (event) => {
    state.polling = event.target.checked;
    updatePollingStatus(state.polling);
  });
  $$("#job-filters button").forEach((button) => {
    button.addEventListener("click", () => setActiveJobFilter(button.dataset.status));
  });
}

async function init() {
  const duration = $("#duration");
  for (let seconds = 4; seconds <= 15; seconds += 1) {
    const option = document.createElement("option");
    option.value = seconds;
    option.textContent = `${seconds} seconds`;
    duration.append(option);
  }
  setupDialogs();
  setupEvents();
  setupNativeClipboard();
  updatePromptCount();
  updateCost();
  state.polling = $("#polling-toggle").checked;
  updatePollingStatus(state.polling);
  renderAttached();
  if (!attempts.isDurable()) {
    $("#context-ir").disabled = true;
    $("#context-ir").title = "Enable browser session storage and reload before billable actions";
    toast(
      "Billable task creation is disabled: browser session storage is unavailable.",
      true,
    );
  }
  await Promise.allSettled([loadHealth(), loadAssets(), loadJobs()]);
  window.setInterval(pollActiveJobs, 8000);
}

init();
