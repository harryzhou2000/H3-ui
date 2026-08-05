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

The app deliberately binds only to a loopback host. See [.env.example](.env.example) for optional
settings.

## Workflow

1. Add reusable text prompts, public URLs, `mm_file://` references, or local media to the source
   shelf. Local assets and metadata persist under `data/`.
2. Attach media, assign its role, and choose duration, ratio, resolution, and watermark settings.
3. Select **Review request**. This validates and previews the exact payload locally; it does not
   call MiniMax.
4. Select **Generate video**, review the published base-price estimate, and explicitly acknowledge
   the billable action. The backend submits that client request at most once.
5. The new task joins the **Active task pool**. Existing queued/running tasks remain in the pool and
   continue polling; a new submission never replaces or cancels anything.
6. When a task succeeds, **Save MP4** refreshes its expiring provider URL and streams it to
   `data/downloads/` before offering a browser download.

MiniMax only permits “Kill” while a task is `queued`. Once a task reaches `running`, the documented
API rejects cancellation, so the UI disables that action. Succeeded and failed remote records can
be explicitly deleted without removing local history or saved outputs.

Local MP4 Base64 requests are supported within the 64 MB body limit. For larger media or MOV input,
choose **Upload to MiniMax** on an asset; this explicitly sends it through the official Files API
and uses an expiring `mm_file://` reference.

## Tests (fake API, no cost)

```bash
uv run pytest
```

The default suite uses `httpx.MockTransport` as a stateful fake MiniMax service. It never uses the
real key or network and makes no billable calls. It covers:

- all-local input organization and validation;
- exact create payloads and explicit spend confirmation;
- multiple submissions accumulating in the active pool;
- duplicate client-request protection;
- polling and signed-URL redaction;
- queued-only kill semantics and running-task refusal;
- result streaming without leaking the Bearer header to the CDN; and
- browser/static secret-boundary checks.

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
- recent-provider task/file reads under `/api/provider/*`
- guarded Context IR and 2K regeneration endpoints

Interactive backend documentation is available at <http://127.0.0.1:8000/api/docs>.

See [docs/API_CONTRACT.md](docs/API_CONTRACT.md) for the official endpoint mapping, validation
rules, and MiniMax file-deletion documentation caveat.
