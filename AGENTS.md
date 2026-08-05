# H3 Studio contributor guide

These instructions apply to the entire repository. Keep this file current when architecture,
provider contracts, safety boundaries, or verification commands change.

<!-- CODEGRAPH_START -->
## CodeGraph

In repositories indexed by CodeGraph (a `.codegraph/` directory exists at the repo root), reach for it BEFORE grep/find or reading files when you need to understand or locate code:

- **MCP tool** (when available): `codegraph_explore` answers most code questions in one call — the relevant symbols' verbatim source plus the call paths between them, including dynamic-dispatch hops grep can't follow. Name a file or symbol in the query to read its current line-numbered source. If it's listed but deferred, load it by name via tool search.
- **Shell** (always works): `codegraph explore "<symbol names or question>"` prints the same output.

If there is no `.codegraph/` directory, skip CodeGraph entirely — indexing is the user's decision.
<!-- CODEGRAPH_END -->

## Product and safety invariants

- This is a Python/FastAPI local-first MiniMax H3 Video Generation V2 workspace. Keep the browser
  free of provider credentials and direct provider URLs.
- Never open, print, copy, commit, or expose `.env`. The server may load it, and the explicitly
  opt-in live test may use it only for the documented read-only task-list request.
- Production Bearer authentication is pinned to `https://api.minimaxi.com`. Tests inject a fake
  `Settings` instance and `httpx.MockTransport`; do not add an environment override for the
  authenticated upstream host.
- The app has one configured loopback origin (host and port). Preserve the client-address, exact
  Host, Origin, CSP, and no-store protections in `app/main.py`.
- Every generation, Context IR, regeneration, upload, cancellation, or deletion needs an explicit
  user action and the existing confirmation appropriate to that operation. Never add automatic
  retries for billable provider POSTs.
- Billable create IDs must survive a reload, remain isolated between tabs, and fail closed when
  browser session storage is unavailable. The SQLite submission ledger must treat ambiguous
  outcomes conservatively and commit provider acceptance plus pool membership atomically.
- New submissions add to the active task pool. They must never replace, cancel, or hide existing
  queued/running tasks. Only an explicit user action may kill a task, and MiniMax permits that only
  while the fresh remote status is `queued`.
- Polling must remain serialized and request-budgeted in the browser, coalesced/rate-limited across
  tabs on the server, and globally honor provider `Retry-After`. Signed output URLs remain
  server-side; never forward the Bearer header to an output CDN.
- Keep spend confirmation text aligned with MiniMax's current public pay-go pricing. If a charge
  cannot be calculated (for example an input video's duration), label it as unknown instead of
  inventing a value.

## Code map

- `app/main.py` — local HTTP boundary, confirmations/idempotency workflow, task lifecycle, polling,
  downloads, and static app mounting.
- `app/provider.py` — purpose-built MiniMax adapter. It is the only provider HTTP layer.
- `app/media.py` — local asset validation and exact provider payload construction.
- `app/db.py` — SQLite assets, additive job pool, and persistent submission ledger.
- `app/schemas.py` — browser-facing request and response contracts.
- `app/static/` — dependency-free browser UI, billing calculations, and task-pool scheduler.
- `tests/` — fake-provider backend tests plus Node tests invoked from pytest.
- `docs/API_CONTRACT.md` — implemented official endpoints, limits, lifecycle, and pricing caveats.

## Environment and verification

Use `uv`; persistent Python packages belong in the repository-local `.venv` and must be declared
and locked in `pyproject.toml` and `uv.lock`.

```bash
uv sync --locked
uv run pytest -q
node --check app/static/app.js
node --test tests/browser_logic.test.mjs
git diff --check
```

The default suite must be offline and cost-free. It must not load `.env` and may use only
fake/mock provider transports.
`RUN_LIVE_MINIMAX_TESTS=1` is exceptional: it is allowed only when explicitly requested, and
`tests/test_live_minimax.py` must remain a single read-only
`GET /v2/query/video_generation?page_num=1&page_size=1` contract probe. Never put create, upload,
regenerate, cancel, delete, or download operations in live tests.

Before committing, verify `.env`, `.venv/`, `.codegraph/`, SQLite files, uploaded assets, downloads,
caches, and credentials are ignored/untracked. Preserve unrelated user changes in a dirty tree.
