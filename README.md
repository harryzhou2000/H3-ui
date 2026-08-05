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

MiniMax only permits “Kill” while a task is `queued`. Once a task reaches `running`, the documented
API rejects cancellation, so the UI disables that action. Succeeded and failed remote records can
be explicitly deleted without removing local history or saved outputs.

MiniMax exposes queryable task history for seven days. If an older locally active task is no longer
available from the provider, H3 Studio marks it `unavailable`, stops polling it, and permits an
explicit local-history removal without claiming that the remote task was cancelled.

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

## API surface

- `GET /api/health`, `POST /api/connection/test`
- local asset CRUD under `/api/assets`, plus explicit `/api/assets/{id}/publish`
- `POST /api/jobs/preview`, `POST /api/jobs`
- `GET /api/jobs`, `POST /api/jobs/{task_id}/refresh`
- `DELETE /api/jobs/{task_id}/remote` and distinct local-history deletion
- `POST /api/jobs/{task_id}/download`, `GET /api/downloads/{filename}`
- explicit `POST /api/provider/tasks` pool sync and read-only `GET /api/provider/files`
- guarded Context IR and 2K regeneration endpoints

Interactive backend documentation is available at <http://127.0.0.1:8000/api/docs>.

See [docs/API_CONTRACT.md](docs/API_CONTRACT.md) for the official endpoint mapping, validation
rules, and MiniMax file-deletion documentation caveat.
