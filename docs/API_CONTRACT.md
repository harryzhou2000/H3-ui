# MiniMax H3 V2 contract implemented by H3 Studio

Verified against the current official MiniMax documentation on 2026-08-05:

- [Create a V2 video task](https://platform.minimaxi.com/docs/api-reference/video-generation-v2-create)
- [Query one task](https://platform.minimaxi.com/docs/api-reference/video-generation-v2-query)
- [List recent tasks](https://platform.minimaxi.com/docs/api-reference/video-generation-v2-list)
- [Cancel or delete a task](https://platform.minimaxi.com/docs/api-reference/video-generation-v2-delete)
- [Create H3 Context IR](https://platform.minimaxi.com/docs/api-reference/video-generation-v2-h3-context-ir)
- [Create a 2K regeneration](https://platform.minimaxi.com/docs/api-reference/video-generation-v2-regeneration)
- [Upload an input file](https://platform.minimaxi.com/docs/api-reference/file-management-upload)

All provider requests use `https://api.minimaxi.com` and server-side Bearer authentication.
The browser has only purpose-built local `/api/*` routes and cannot choose an upstream host, path,
method, header, or key.

Reusable prompt formats and media-label ordering are documented in:

- [REF2VA prompt template](H3_REF2VA_PROMPT_TEMPLATE.md)
- [I2VA and FL2VA prompt templates](H3_I2VA_FL2VA_PROMPT_TEMPLATE.md)

The browser persists only opaque billable-attempt IDs in per-tab session storage so a reload after
a lost response reuses the backend ledger entry without another tab overwriting it. Billable task
creation fails closed if storage cannot be verified. The server accepts one exact configured
loopback origin, preventing origin aliases or ports from splitting that storage boundary.

Ambiguous provider outcomes remain blocked for reconciliation; only an explicit allowlist of
definite client rejections can retry without changing the ID. Provider acceptance and additive-pool
membership commit in one SQLite transaction. Active-task polling is serialized, concurrency-capped,
request-budgeted, and least-recently-polled in the browser; the server coalesces same-task reads,
limits poll starts across tabs, and applies `Retry-After` globally.
The local pool-sync route is a same-origin `POST /api/provider/tasks`, even though its upstream
operation is read-only, because a successful sync persists provider tasks into local history.

## Implemented lifecycle

| User operation | MiniMax operation | Spend/mutation guard |
| --- | --- | --- |
| Review request | None | Builds and validates a redacted payload locally |
| Generate | `POST /v2/video_generation` | Explicit confirmation and persistent client request ledger |
| Poll one | `GET /v2/query/video_generation/{task_id}` | Read-only; a finished video is immediately secured locally |
| Sync pool | `GET /v2/query/video_generation` | Read-only; last seven days |
| Kill queued task | `DELETE /v2/video_generation/{task_id}` | Fresh-state check plus explicit confirmation |
| Delete succeeded/failed record | same `DELETE` endpoint | Fresh-state check; local copy/history remains |
| Save output | serve the automatic local copy, or refresh and retry it | No Bearer header is sent to the CDN |
| Upload local input | `POST /v1/files/upload` with `purpose=video_generation_input` | Explicit authorization; seven-day provider TTL |
| Context IR | `POST /v2/h3_context_ir` | Marked token-billed and explicitly confirmed |
| Regenerate | `POST /v2/video_regeneration` | Marked billable and explicitly confirmed |

The provider statuses are `queued`, `running`, `succeeded`, `failed`, and `cancelled`. Only a
`queued` task may be cancelled. A `running` task cannot be killed through the documented API.
Succeed/failed task records may be deleted. H3 Studio never cancels an old task when a new one is
submitted: active tasks form an additive pool.

When a refresh first observes a succeeded video task, the server immediately downloads the MP4
before completing that refresh. A bulk provider sync starts up to two automatic downloads at once
without exposing or persisting signed result URLs. Downloads use deterministic filenames and
temporary `.part` files, so polling from multiple tabs and explicit Save/Preview actions reuse the
same local result instead of downloading it again. Context IR text results are retained as text and
do not enter this video-download path. A temporary CDN or disk failure does not change the provider's
succeeded status; the next uncached refresh or explicit Save retries with a fresh result URL.

MiniMax task queries cover the most recent seven days. A local queued/running record older than
that window that receives a provider invalid/not-found response becomes local status `unavailable`.
It leaves the polling pool and can be removed from local history; H3 Studio does not represent that
transition as a remote cancellation or deletion.

## Pricing shown before billable calls

The confirmation copy follows the public [MiniMax pay-as-you-go pricing](https://platform.minimaxi.com/docs/guides/pricing-paygo):

- H3 output: ¥0.50/second at 768P or ¥0.80/second at 2K.
- Generation inputs: audio free; first five images free, then ¥0.20/image; input video charged per
  input second at the selected output-resolution rate.
- 768P→2K regeneration output: ¥0.30/second.
- Regeneration re-bills original inputs: audio free; first five images free, then ¥0.15/image;
  input video ¥0.30/input second.

H3 Studio can count images but does not pretend to know every local or remote input-video duration.
It therefore labels the calculated output/image subtotal and explicitly identifies unestimated
input-video charges instead of presenting that subtotal as a final price.
Regeneration from a synced task is disabled until MiniMax supplies the exact 4–15 second source
duration; the UI never substitutes a cheaper default duration.

## Request rules enforced before submission

- Model is fixed to `MiniMax-H3`.
- Exactly one non-empty prompt, at most 7,000 characters.
- Resolution is `768P` or `2K`; duration is an integer from 4 through 15 seconds.
- Text-only requests require a concrete ratio. Frame-based provider requests use `adaptive`.
  Choosing a concrete ratio for a local JPEG/PNG/WebP first/last frame performs a centered local
  crop and locally downsizes oversized images without stretching, then sends the processed frame
  with provider ratio `adaptive`; remote and `mm_file://` frames are never fetched for cropping.
  Reference requests may use `adaptive` or a concrete supported ratio.
- Frame roles and reference roles cannot mix.
- At most one first frame, one last frame, nine reference images, three reference videos, and three
  reference audio clips. Reference audio cannot be the only reference medium.
- Local per-file size and extension limits follow the official image/video/audio limits. The final
  JSON request is capped at 64 MB. Local MOV inputs must first use the official file upload route,
  because V2 only documents a Base64 data URI for MP4 video.

Container codec, dimensions, media duration, aspect, and frame-rate checks are authoritative when
MiniMax validates `video_generation_input`; this app does not pretend extension inspection proves
those properties.

## Local image editing

`POST /api/assets/{asset_id}/resize` accepts a concrete supported `ratio` and a `max_edge` from 32
through 4096 pixels. It operates only on local JPEG, PNG, or WebP image assets, center-crops and
downscales with Pillow, stores a new local asset record and file, and leaves the original unchanged.
It never fetches a remote asset or calls the provider.

## File deletion documentation caveat

MiniMax documents `video_generation_input` for upload and list, but its file-delete purpose enum
does not list that input purpose. H3 Studio therefore does not issue an ambiguous remote input-file
delete. It deletes only the local asset and lets an explicitly uploaded provider input expire after
its documented seven-day lifetime.
