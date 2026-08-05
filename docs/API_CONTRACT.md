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

## Implemented lifecycle

| User operation | MiniMax operation | Spend/mutation guard |
| --- | --- | --- |
| Review request | None | Builds and validates a redacted payload locally |
| Generate | `POST /v2/video_generation` | Explicit confirmation and persistent client request ledger |
| Poll one | `GET /v2/query/video_generation/{task_id}` | Read-only; signed result URL stays on the server |
| Sync pool | `GET /v2/query/video_generation` | Read-only; last seven days |
| Kill queued task | `DELETE /v2/video_generation/{task_id}` | Fresh-state check plus explicit confirmation |
| Delete succeeded/failed record | same `DELETE` endpoint | Fresh-state check; local copy/history remains |
| Save output | refresh task, then stream `task.content.url` | No Bearer header is sent to the CDN |
| Upload local input | `POST /v1/files/upload` with `purpose=video_generation_input` | Explicit authorization; seven-day provider TTL |
| Context IR | `POST /v2/h3_context_ir` | Marked token-billed and explicitly confirmed |
| Regenerate | `POST /v2/video_regeneration` | Marked billable and explicitly confirmed |

The provider statuses are `queued`, `running`, `succeeded`, `failed`, and `cancelled`. Only a
`queued` task may be cancelled. A `running` task cannot be killed through the documented API.
Succeed/failed task records may be deleted. H3 Studio never cancels an old task when a new one is
submitted: active tasks form an additive pool.

## Request rules enforced before submission

- Model is fixed to `MiniMax-H3`.
- Exactly one non-empty prompt, at most 7,000 characters.
- Resolution is `768P` or `2K`; duration is an integer from 4 through 15 seconds.
- Text-only requests require a concrete ratio. Frame-based requests use `adaptive`. Reference
  requests may use `adaptive` or a concrete supported ratio.
- Frame roles and reference roles cannot mix.
- At most one first frame, one last frame, nine reference images, three reference videos, and three
  reference audio clips. Reference audio cannot be the only reference medium.
- Local per-file size and extension limits follow the official image/video/audio limits. The final
  JSON request is capped at 64 MB. Local MOV inputs must first use the official file upload route,
  because V2 only documents a Base64 data URI for MP4 video.

Container codec, dimensions, media duration, aspect, and frame-rate checks are authoritative when
MiniMax validates `video_generation_input`; this app does not pretend extension inspection proves
those properties.

## File deletion documentation caveat

MiniMax documents `video_generation_input` for upload and list, but its file-delete purpose enum
does not list that input purpose. H3 Studio therefore does not issue an ambiguous remote input-file
delete. It deletes only the local asset and lets an explicitly uploaded provider input expire after
its documented seven-day lifetime.
