const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const ACTIVE_STATUSES = new Set(["queued", "running"]);
const TERMINAL_STATUSES = new Set(["succeeded", "failed", "cancelled", "deleted"]);

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
  nextPollAt: new Map(),
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
  const item = document.createElement("div");
  item.className = `toast${isError ? " is-error" : ""}`;
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

function makeButton(label, className, onClick, disabled = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.disabled = disabled;
  if (onClick) button.addEventListener("click", onClick);
  return button;
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
    if (asset.kind === "image" && asset.preview_url) {
      const image = document.createElement("img");
      image.src = asset.preview_url;
      image.alt = "";
      image.loading = "lazy";
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
  for (const attached of state.attached) {
    const asset = state.assets.find((item) => item.id === attached.assetId);
    if (!asset) continue;
    const row = document.createElement("div");
    row.className = "attached-item";
    const name = document.createElement("strong");
    name.textContent = asset.name;
    name.title = asset.name;
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
      updateMode();
      invalidatePreview();
    });
    const remove = makeButton("×", "remove-attached", () => {
      state.attached = state.attached.filter((item) => item !== attached);
      enforceRatioMode();
      renderAttached();
      invalidatePreview();
    });
    remove.setAttribute("aria-label", `Remove ${asset.name}`);
    row.append(name, select, remove);
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
  ratio.disabled = frameMode;
  if (frameMode) ratio.value = "adaptive";
  if (!frameMode && !state.attached.length && ratio.value === "adaptive") ratio.value = "16:9";
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

function buildGeneration(confirmed = false, clientRequestId = `preview-${crypto.randomUUID()}`) {
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

function buildContextIR(confirmed = false, clientRequestId = `preview-${crypto.randomUUID()}`) {
  const generation = buildGeneration(confirmed, clientRequestId);
  delete generation.resolution;
  delete generation.aigc_watermark;
  return generation;
}

function invalidatePreview() {
  state.validatedSignature = null;
  state.lastPreview = null;
  $("#generate").disabled = true;
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
    $("#generate").disabled = false;
    showValidation(
      true,
      "Request is valid",
      `${formatBytes(preview.estimated_request_bytes)} JSON payload. Generation is still blocked until explicit confirmation.`,
    );
  } catch (error) {
    state.validatedSignature = null;
    $("#generate").disabled = true;
    $("#request-json").textContent = `Validation failed: ${error.message}`;
    showValidation(false, "Fix this scene before submitting", error.message);
  } finally {
    setBusy(button, false);
  }
}

function estimatedCost() {
  const rate = $("#resolution").value === "2K" ? 0.8 : 0.5;
  return rate * Number($("#duration").value);
}

function updateCost() {
  $("#cost-inline").textContent = `from ¥${estimatedCost().toFixed(2)}`;
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
    const onClose = () => {
      check.removeEventListener("change", onChange);
      dialog.removeEventListener("close", onClose);
      resolve(dialog.returnValue === "default" && check.checked);
    };
    check.addEventListener("change", onChange);
    dialog.addEventListener("close", onClose);
    dialog.showModal();
  });
}

async function generateVideo() {
  if (state.submitting) return;
  if (state.validatedSignature !== composerSignature()) {
    toast("Review the current request before generating.", true);
    return;
  }
  const duration = Number($("#duration").value);
  const resolution = $("#resolution").value;
  const confirmed = await confirmOperation({
    title: "Add this job to the pool?",
    description: "This creates a new MiniMax task. Every existing active task continues running; nothing is replaced or cancelled.",
    summary: [
      { value: resolution, label: "Resolution" },
      { value: `${duration}s`, label: "Duration" },
      { value: `¥${estimatedCost().toFixed(2)}+`, label: "Published base" },
    ],
    label: "I understand this is billable and creates one additional active task.",
    accent: "Generate & add",
  });
  if (!confirmed) return;

  const button = $("#generate");
  state.submitting = true;
  setBusy(button, true, "Submitting once…");
  try {
    const requestId = `generation-${crypto.randomUUID()}`;
    const result = await api("/api/jobs", {
      method: "POST",
      body: buildGeneration(true, requestId),
    });
    toast(`Task ${result.task_id} joined the active pool.`);
    state.jobFilter = "active";
    setActiveJobFilter("active");
    await loadJobs();
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.submitting = false;
    setBusy(button, false);
    button.disabled = state.validatedSignature !== composerSignature();
  }
}

async function createContextIR() {
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
    const result = await api("/api/context-ir", {
      method: "POST",
      body: buildContextIR(true, `context-ir-${crypto.randomUUID()}`),
    });
    toast(`Context IR task ${result.task_id} joined the pool.`);
    await loadJobs();
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(button, false);
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
    invalidatePreview();
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

function jobTask(job) {
  return job.response?.task || {};
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
    button.classList.toggle("is-active", button.dataset.status === filter);
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
      if (task.modality !== "text" && job.operation !== "h3_context_ir") {
        actions.append(makeButton("Save MP4", "micro-button", () => saveOutput(job)));
      }
      if (settings.resolution === "768P" && job.operation === "generation") {
        actions.append(makeButton("Regenerate 2K", "micro-button", () => regenerateJob(job)));
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

async function refreshJob(taskId, quiet = false) {
  if (state.pollInFlight.has(taskId)) return;
  state.pollInFlight.add(taskId);
  try {
    await api(`/api/jobs/${encodeURIComponent(taskId)}/refresh`, { method: "POST" });
    if (!quiet) toast(`Task ${taskId} refreshed.`);
    return true;
  } catch (error) {
    if (!quiet) toast(error.message, true);
    const retry = Number(error.detail?.retry_after || error.detail?.detail?.retry_after || 0);
    state.nextPollAt.set(taskId, Date.now() + Math.max(15, retry || 15) * 1000);
    return false;
  } finally {
    state.pollInFlight.delete(taskId);
  }
}

async function pollActiveJobs() {
  if (!state.polling || document.hidden) return;
  const now = Date.now();
  const active = state.jobs.filter(
    (job) => ACTIVE_STATUSES.has(job.status) && (state.nextPollAt.get(job.task_id) || 0) <= now,
  );
  if (!active.length) return;
  const results = await Promise.all(active.map((job) => refreshJob(job.task_id, true)));
  if (results.some(Boolean)) await loadJobs();
}

async function syncProviderTasks() {
  const button = $("#sync-tasks");
  button.classList.add("is-spinning");
  button.disabled = true;
  try {
    const result = await api("/api/provider/tasks?page_num=1&page_size=100");
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
  const task = jobTask(job);
  const duration = Number(task.duration || job.request?.duration || 4);
  const confirmed = await confirmOperation({
    title: "Add a 2K regeneration task?",
    description: "This billed operation creates another task from the eligible 768P source. Source-task regeneration may require MiniMax whitelist access.",
    summary: [
      { value: "768P → 2K", label: "Operation" },
      { value: `${duration}s`, label: "Source duration" },
      { value: `¥${(duration * 0.8).toFixed(2)}+`, label: "Published base" },
    ],
    label: "I understand this creates an additional billed regeneration task.",
    accent: "Regenerate in 2K",
  });
  if (!confirmed) return;
  try {
    const result = await api("/api/regenerations", {
      method: "POST",
      body: {
        client_request_id: `regeneration-${crypto.randomUUID()}`,
        model: "MiniMax-H3",
        source_task_id: job.task_id,
        resolution: "2K",
        aigc_watermark: false,
        confirmed: true,
      },
    });
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
}

function setupEvents() {
  $("#test-connection").addEventListener("click", testConnection);
  $("#file-input").addEventListener("change", (event) => uploadFiles([...event.target.files]));
  $("#asset-search").addEventListener("input", renderAssets);
  $$("#asset-filters button").forEach((button) => {
    button.addEventListener("click", () => {
      state.assetKind = button.dataset.kind;
      $$("#asset-filters button").forEach((item) => item.classList.toggle("is-active", item === button));
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
  dropZone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") $("#file-input").click();
  });

  $("#prompt").addEventListener("input", () => {
    updatePromptCount();
    invalidatePreview();
  });
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
    $(".polling-row > span").style.opacity = state.polling ? "1" : "0.45";
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
  updatePromptCount();
  updateCost();
  renderAttached();
  await Promise.allSettled([loadHealth(), loadAssets(), loadJobs()]);
  window.setInterval(pollActiveJobs, 8000);
}

init();
