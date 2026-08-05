# H3 Studio

H3 Studio is a local-first web workspace for the current MiniMax H3 Video Generation V2 API. It
organizes text, image, video, and audio inputs; builds valid multimodal requests; keeps an additive
pool of asynchronous tasks; polls every active task; and saves completed MP4 files locally.

The Python server is the only component that can read `.env` or contact MiniMax. The browser never
receives the API key and contains no direct provider URL.

## Start

```bash
uv sync
uv run python -m app.main
```

Open <http://127.0.0.1:8000>. `uv sync` creates the requested local `.venv`; both `.venv` and `.env`
are git-ignored.

The existing `.env` format is supported:

```dotenv
export MINIMAX_API_KEY=your-key-here
```

The app deliberately binds to one exact loopback origin (configured host and port) and rejects
other loopback aliases, ports, clients, or `Host` headers even through another ASGI runner. Open
the exact URL printed at startup. The authenticated provider
host is pinned to `https://api.minimaxi.com`; it cannot be redirected with an environment variable.
See [.env.example](.env.example) for optional local runtime settings.

## Workflow

1. Add reusable text prompts, public URLs, `mm_file://` references, or local media to the source
   shelf. Local assets and metadata persist under `data/`.
2. Attach media, assign its role, and choose duration, ratio, resolution, and watermark settings.
   Attached local images show a thumbnail. In first/last-frame mode, `adaptive` preserves the
   source shape; choosing a concrete ratio center-crops local JPEG/PNG/WebP frames and downsizes
   oversized images locally without stretching them. Remote and MiniMax file-ID frames are never
   fetched locally for cropping.
3. Select **Review request**. This validates and previews the exact payload locally; it does not
   call MiniMax.
4. Select **Generate video**, review the published base-price estimate, and explicitly acknowledge
   the billable action. Per-tab session storage keeps the same request ID across a reload or
   timeout/retry; if durable storage is unavailable, billable creation fails closed. The backend's
   persistent ledger submits that client request at most once.
5. The new task joins the **Active task pool**. Existing queued/running tasks remain in the pool and
   continue polling; a new submission never replaces or cancels anything.
6. When a task succeeds, **Save MP4** refreshes its expiring provider URL and streams it to
   `data/downloads/` before offering a browser download.

Any local task card with a stored request has **Load workspace**. It restores the prompt, surviving
source attachments and roles, output settings, and frame crop choice, then rotates to a fresh
idempotency intent. Editing a source-shelf item can update names and metadata, text-prompt bodies,
or public URL/file-ID values as appropriate. Image sources get an image-specific editor with a
preview; local JPEG/PNG/WebP images can create a center-cropped, downsized copy at a selected aspect
ratio and maximum edge while preserving the original. Native clipboard shortcuts are preserved in
all text fields, with explicit Copy/Paste controls on the main prompt.

Input videos with a local or public URL have **Preview video**, which opens the browser's native
player in a separate window. Completed output previews first create or reuse a local MP4 copy, then
open that local file in a new window; expiring signed provider URLs and Bearer credentials never
reach the preview window. Completed Context IR tasks expose their full returned text with Copy and
**Use as Direction** actions. Errors use a dedicated top-layer dialog so an open editor or
confirmation window cannot hide them.

MiniMax only permits “Kill” while a task is `queued`. Once a task reaches `running`, the documented
API rejects cancellation, so the UI disables that action. Succeeded and failed remote records can
be explicitly deleted without removing local history or saved outputs.

MiniMax exposes queryable task history for seven days. If an older locally active task is no longer
available from the provider, H3 Studio marks it `unavailable`, stops polling it, and permits an
explicit local-history removal without claiming that the remote task was cancelled.

Assets, submissions, and every queued/running task are committed to `data/studio.db` (SQLite/WAL).
Only short-lived poll locks and rate-limit timers live in RAM, so the full active pool reloads after
a server restart and resumes polling when the browser reconnects.

Local MP4 Base64 requests are supported within the 64 MB body limit. For larger media or MOV input,
choose **Upload to MiniMax** on an asset; this explicitly sends it through the official Files API
and uses an expiring `mm_file://` reference.

## Pricing disclosure

Confirmations follow MiniMax's current [pay-as-you-go video pricing](https://platform.minimaxi.com/docs/guides/pricing-paygo):
H3 output is ¥0.50/second at 768P or ¥0.80/second at 2K; the first five input images
are free and additional images are ¥0.20 each; input video is billed per input second at the
selected output rate; audio is free. A 768P→2K regeneration is ¥0.30/output second and re-bills
the original inputs (additional images ¥0.15 each and input video ¥0.30/second). The UI separately
shows known charges and flags video-duration charges it cannot calculate locally.

## Tests (fake API, no cost)

```bash
uv run pytest
```

The default suite uses `httpx.MockTransport` as a stateful fake MiniMax service. It never uses the
real key or network and makes no billable calls. It covers:

- all-local input organization and validation;
- exact create payloads and explicit spend confirmation;
- multiple submissions accumulating in the active pool;
- stable browser retry IDs and persistent duplicate client-request protection;
- serialized, rate-budgeted polling that eventually visits every eligible active task;
- cross-tab poll coalescing, a process-wide provider rate cap, and global `Retry-After` backoff;
- atomic insertion of every accepted task into the additive pool;
- seven-day unavailable-task archival and exact-duration regeneration guards;
- polling and signed-URL redaction;
- queued-only kill semantics and running-task refusal;
- result streaming without leaking the Bearer header to the CDN; and
- browser/static secret-boundary checks.

The browser-pool checks run as a small Node test from pytest; Node 20 or newer is therefore needed
when running the complete development test suite. It is not needed to run H3 Studio itself.

An optional live probe verifies only authentication and the current task-list response shape:

```bash
RUN_LIVE_MINIMAX_TESTS=1 uv run pytest tests/test_live_minimax.py -q
```

That probe performs `GET /v2/query/video_generation?page_num=1&page_size=1`, discards task data, and
does not create, cancel, upload, delete, or regenerate anything. Live billable POST operations are
never part of the test suite.

## Development safeguards

Install the repository-managed hook after `uv sync`:

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

The hook uses tools locked into the local `.venv`; it trims whitespace, validates YAML/TOML and
JavaScript syntax, runs Ruff lint/format, rejects conflict markers, private keys, oversized files,
and likely secrets. The same checks and fake-only test suite run in GitHub Actions. No hook or
default CI step loads `.env` or calls MiniMax.

Provider-independent application code targets the `VideoProvider` protocol in
`app/providers/base.py`. Trusted adapters are registered by code in `app/providers/factory.py`;
tests and future local AI components can instead inject any compatible provider directly into
`create_app`. The built-in MiniMax adapter remains in `app/provider.py`, and its authenticated host
remains pinned rather than being selected from user-controlled provider URLs.

## API surface

- `GET /api/health`, `POST /api/connection/test`
- local asset CRUD under `/api/assets`, local-copy image resizing under
  `/api/assets/{id}/resize`, plus explicit `/api/assets/{id}/publish`
- `POST /api/jobs/preview`, `POST /api/jobs`
- `GET /api/jobs`, `POST /api/jobs/{task_id}/refresh`
- `DELETE /api/jobs/{task_id}/remote` and distinct local-history deletion
- `POST /api/jobs/{task_id}/download`, `GET /api/downloads/{filename}`
- explicit `POST /api/provider/tasks` pool sync and read-only `GET /api/provider/files`
- guarded Context IR and 2K regeneration endpoints

Interactive backend documentation is available at <http://127.0.0.1:8000/api/docs>.

See [docs/API_CONTRACT.md](docs/API_CONTRACT.md) for the official endpoint mapping, validation
rules, and MiniMax file-deletion documentation caveat.
