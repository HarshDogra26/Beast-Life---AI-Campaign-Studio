# AI Campaign Creative Studio

Product brief → research agent (real web tools) → 3 source-backed angles → you select one → one
shared creative specification → **1080×1080 ad + 1080×1920 ad + a 6–10s vertical MP4**.

Drafts for Meta placements. Nothing is published anywhere.

---

## 1. Run and verify

### Setup

```bash
# backend
cd backend
python -m pip install -e ".[dev]"
cp .env.example .env
python -m uvicorn app.main:app --port 8000

# frontend (second terminal)
cd frontend
npm install && npm run dev            # http://localhost:5173
```

Requires Python 3.11+, Node 20+, and FFmpeg (`winget install Gyan.FFmpeg` / `apt install ffmpeg`).
`/api/health` reports `ffmpeg: MISSING` if absent and the UI shows a banner — research and both
image ads still work without it; only the video stage fails.

### Environment variables

Full template with comments: **[backend/.env.example](backend/.env.example)**. The ones that matter:

| Variable | Purpose |
|---|---|
| `PROVIDER_MODE` | `fixture` (default, offline) or `live` |
| `AZURE_OPENAI_ENDPOINT` / `_API_KEY` | Azure resource |
| `AZURE_OPENAI_CHAT_DEPLOYMENT` | e.g. `gpt-4.1` |
| `AZURE_OPENAI_IMAGE_DEPLOYMENT` | **must match a deployment that exists** — see §6 |
| `TAVILY_API_KEY` | read only by the MCP server process |
| `MAX_SEARCH_CALLS` / `MAX_FETCH_CALLS` / `RESEARCH_WALL_CLOCK_S` | agent budget |
| `IMAGE_RPM` | image rate limit (5 = Azure's default quota) |
| `FAILURE_INJECT` | reproducible failures, see below |

Secrets stay server-side; `.env` is gitignored.

### Fixture mode

Default. Replays recorded Tavily responses ([tests/fixtures/tavily/](backend/tests/fixtures/tavily/))
and renders imagery locally, so the entire flow runs with **no keys and no network**. It is labelled
as fixture in `/api/health`, on the campaign row, with a `[FIXTURE]` prefix on every source title,
and as a UI banner. Fixture research is never presented as live browsing.

### Tests and verification

```bash
cd backend
python -m pytest -q                   # 114 tests
python scripts/verify_campaign.py     # end-to-end, 28 checks
```

[`verify_campaign.py`](backend/scripts/verify_campaign.py) drives the real HTTP API and asserts every
acceptance criterion independently — image dimensions measured with Pillow from the *downloaded
bytes*, video duration and dimensions measured with `ffprobe`, sources checked for title/URL/access
time, downloads checked for content, and the campaign re-read to confirm it survives.

### Reproducing a failure and retry

```bash
FAILURE_INJECT=render_video:timeout python -m uvicorn app.main:app --port 8000
```

Syntax `stage:kind` or `stage:kind*N` (fail N times then succeed). Kinds: `429`, `timeout`, `500`,
`content_policy`. The UI banners while active.

Then in the UI: the video stage fails, both images remain intact, and the stage shows a **Retry**
button. Restart without `FAILURE_INJECT` and retry — measured result:

```
before retry:  11 provider calls, 3 image calls
after retry:   11 provider calls, 3 image calls     ← 0 additional
stages:        8 upstream stages "reused", render_video "completed" (attempt 2)
video:         1080x1920, 8.0s
```

---

## 2. Architecture

### Workflow

```
validate_brief
  └─> research                  bounded tool loop over MCP
        └─> synthesize_angles   structured output, citations validated
              └─> await_selection   interrupt()  ◀── HUMAN PAUSE
                    └─> build_spec          CampaignSpec v1 + claim guard
                          └─> master_scene  ONE image call — identity anchor
                                ├─> render_square    ┐ CONCURRENT
                                └─> render_vertical  ┘
                                      └─> render_video   waits on vertical
```

- **Concurrent:** the two renders depend only on the master scene, not on each other, so LangGraph
  schedules them in one superstep.
- **Must wait:** the video consumes the finished 1080×1920 asset.
- **Sequential by nature:** research — each search depends on the previous result.

`GET /api/workflow` returns this as JSON. Edge list: [graph/builder.py](backend/app/graph/builder.py).

### Stage contracts

Pure Pydantic, no I/O, in [app/domain/](backend/app/domain/) — the dependency sink; nothing in it
imports `providers`, `storage` or `graph`.

| Contract | Guarantees beyond schema |
|---|---|
| [`ProductBrief`](backend/app/domain/brief.py) | CTA ≤ 8 words; control chars stripped; `reference_image_id` can't traverse paths |
| [`ResearchReport`](backend/app/domain/research.py) | every cited `source_id` resolves to a fetched page; performance vocabulary rejected in interpretation fields; <3 sources forces a `coverage_gap` |
| [`CampaignSpec`](backend/app/domain/spec.py) | claim guard (no percentages, certifications, discounts, superlatives, health claims); video beats total 6–10s; both formats have composition guidance |
| [`RenderedAsset`](backend/app/domain/assets.py) | exact export dimensions; video duration in range |

Schema constrains the model at generation time; Pydantic enforces what a schema cannot express.
Both are needed — a JSON schema cannot know which source ids exist.

### Data model

[`app/storage/models.py`](backend/app/storage/models.py) — `campaigns`, `stage_runs`, `sources`,
`research_reports`, `campaign_specs`, `assets`, `jobs`, `idempotency_keys`, `provider_calls`.

Two persistence layers, deliberately: LangGraph's `AsyncSqliteSaver` owns suspension/resumption (the
`interrupt()`); these tables own everything queryable. The history screen never deserialises a
checkpoint blob.

### Agent tools

A FastMCP server ([mcp_servers/research_server.py](backend/mcp_servers/research_server.py)) exposing
**exactly two read-only tools**: `web_search` (Tavily `/search`) and `fetch_page` (Tavily `/extract`).
In-process by default; `MCP_INPROCESS=false` runs it separately, which keeps the Tavily key out of
the application process.

Budget is enforced in [research_tools.py](backend/app/graph/research_tools.py) before each dispatch —
a prompt-level limit is advisory, a counter that refuses the call is a guarantee.

**Untrusted content.** Page text is evidence, never instructions. The control that matters is that
there is nothing to actuate: both tools are read-only, and angle synthesis runs as a separate call
with **no tools bound**. Beyond that: fenced quoting with fence tokens neutralised first (leaving an
auditable `[removed]` marker), zero-width/bidi stripping, and heuristic flags surfaced in the UI.
The fixture corpus contains a page demanding `SYSTEM COMPROMISED`;
[test_security.py](backend/tests/test_security.py) asserts it *is* retained as evidence and changed
nothing in the output.

### Recovery and retry

**Reuse.** `stage_runs` is keyed `(campaign_id, stage)` with an **input fingerprint** (sha256 of the
upstream contract JSON). A stage that is COMPLETED *and* whose fingerprint still matches is not
re-executed. The fingerprint matters as much as the status — status alone would reuse a render built
from a spec the user has since edited. Retry resets only the named stage.

**Restart.** Every RUNNING row records a `worker_epoch`. At startup, rows from a different epoch are
provably orphaned and marked `INTERRUPTED` with a reason — so killed work is retryable rather than a
permanent spinner.

**Duplicate submission.** `Idempotency-Key` replays the original response; the server also refuses a
second generate/retry while a job is in flight (409). **Residual risk, stated plainly:** a provider
timeout *after* Azure generated and billed an image is not recoverable client-side, and Azure OpenAI
documents no idempotency key for `/images/*`. The ledger writes a row *before* dispatch and settles
it after, so an unexplained charge stays visible as `unsettled_calls` rather than disappearing.

**Storage.** GPT-image returns base64, never URLs, so there are no temporary provider URLs to expire.
Bytes go to a content-addressed local store immediately.

---

## 3. Creative approach

### How research informed the angle

From the live run (`c_460f128f27b766be0721`), the agent chose its own queries, read three pages, and
ran a second narrower search after noticing its first results clustered on one theme:

| | |
|---|---|
| Sources | `garagegymreviews.com/fitness-habits`, `pmc.ncbi.nlm.nih.gov/articles/PMC7497044`, `puregym.com/blog/uk-fitness-report-gym-statistics` |
| Budget used | 2/4 searches, 3/6 fetches, 28.7s of 120s |
| Selected angle | `a1` — *"Power up your evening routine."* |
| Its sourced insight | 25–40s fit training around full-time work; 54% of 25–34s cite a specific barrier — **cited to `s3`**, which is where that figure actually appears |
| Resulting headline | *"Unflavoured protein, ready for your evening routine."* |

The structural point: `audience_insight` **must** cite a source id that resolves to a page actually
fetched, while `visual_direction` and `rationale` are marked as interpretation and are *rejected* if
they contain performance vocabulary. Sourced and invented are separated by a validator, not by a
request in a prompt.

### How both images stay consistent

Two unrelated text-to-image calls would produce two different products in two different worlds.
Instead ([graph/nodes/images.py](backend/app/graph/nodes/images.py)):

1. **One master scene**, generated once at 1024×1024 — the one size every GPT-image model accepts, so
   the anchor is identical on either size strategy. With an uploaded packshot this is instead an
   `/images/edits` call on that packshot, anchoring identity to the real product.
2. **Each format re-frames that scene** via `/images/edits` **with the master scene passed in as the
   input image** — not a fresh prompt. The model recomposes an existing photograph, so product,
   palette and lighting carry across by construction.
3. **Copy is composited deterministically** by a Pillow layout engine from the single `CampaignSpec`,
   never drawn by the image model. The headline is therefore exactly the approved wording, spelled
   correctly, at a *measured* WCAG contrast ratio — scrim opacity is computed by raising it until the
   blended backdrop clears 4.5:1.

**The size constraint that shaped this.** GPT-image custom sizes require both edges to be multiples
of 16, and **1080 is not** (1080 ÷ 16 = 67.5). The export targets are therefore ungeneratable. What
*is* generatable is the exact aspect:

| Asset | Generated | → Export |
|---|---|---|
| Square | `1088×1088` (×16, exact 1:1) | ×0.993 → **1080×1080** |
| Vertical | `1152×2048` (×16, **exact 9:16**) | ×0.9375 → **1080×1920** |

Exact 9:16 with both edges divisible by 16 requires `w=144m, h=256m`; `m=7` would force an upscale,
so `m=8` is the smallest that works — proven in [test_sizes.py](backend/tests/test_sizes.py). Both
are pure LANCZOS downscales: no crop, no letterbox, no stretch. A startup capability probe degrades
to a canvas-extension strategy for deployments locked to the legacy 1024 size set.

### How the video is rendered

Entirely by the application pipeline ([rendering/video.py](backend/app/rendering/video.py)):

1. Pillow builds one keyframe per storyboard beat from the **copy-free plate** of the approved
   1080×1920 asset. (The plate exists because animating the finished ad would double-expose its
   baked-in headline against the video's own per-beat text — a defect found by inspecting output
   frames.)
2. FFmpeg applies a slow zoompan per beat, `xfade` crossfades, then `libx264 -crf 20 -pix_fmt
   yuv420p -movflags +faststart`.
3. **`ffprobe` measures the result** and the stage *fails* if duration or dimensions fall outside
   contract, rather than shipping a quietly wrong asset.

Duration arithmetic: `xfade` overlaps clips, so output = `sum(durations) − (n−1)×transition`. Every
clip but the last is extended by one transition length so the overlaps cancel and the output lands on
exactly the spec's total. Live run: 4 beats, **8.00s**, 1080×1920, h264.

---

## 4. Decisions

### A hand-written research loop, not a prebuilt agent

**Alternative:** LangGraph's `create_agent` with MCP tools bound.
**Chosen:** an explicit loop in [nodes/research.py](backend/app/graph/nodes/research.py).

Three properties were worth more than the convenience: bounds enforced at the dispatch point rather
than requested in a prompt; every turn's stated decision and tool call recorded as a first-class
artifact, since the trace *is* a deliverable; and termination controlled by us (wall clock, turn
count, budget) rather than by the model choosing to stop.

**Trade-off accepted:** roughly 120 lines that a framework would have provided, and the loop must be
maintained as provider APIs change. Worth it — the assignment grades observability and bounds, and
both are properties of this loop.

### SQLite plus an in-process asyncio worker, not Redis/Celery

**Alternative:** Celery + Redis, or a managed queue.
**Chosen:** a durable `jobs` table and a single asyncio consumer ([worker/runner.py](backend/app/worker/runner.py)).

The property that actually matters is that work survives a restart, and that comes from the job
*row*, not the queue technology. A single worker is also correct given a 5-images/minute quota —
concurrent campaigns would mostly produce 429s. WAL mode keeps worker writes and API reads from
colliding.

**Trade-off accepted:** no horizontal scaling, and SQLite write throughput caps concurrency.

### Highest-priority production limitations

1. **Single-process worker with SQLite.** Would need Postgres plus a real queue before more than one
   instance. The swap points are deliberately narrow: `checkpointer_context()` and `JobRepository`.
2. **Duplicate paid work on provider timeout** — see §2. The ledger makes it *visible*, not
   impossible. Real fix needs provider-side idempotency, which Azure does not document for images.
3. **No auth, no tenancy.** Every campaign is world-readable to anyone who can reach the API.
4. **Artifacts on local disk.** No object store, no lifecycle policy, no CDN.

---

## 5. AI-assisted development

> **Note for the reviewer:** this section describes the actual workflow used. Adjust it if your
> recollection differs — do not submit a description of a process you did not follow.

**Tool:** Claude Code (Claude Opus 5) in the VS Code extension, one long session.

**Task breakdown.** Planning first: the assignment text was given verbatim, then the agent asked
clarifying questions before writing any code — provider stack, orchestrator, video pipeline, and the
9:16 strategy. It researched Azure's current image-model reference *before* planning and found two
facts that changed the design (below). A written plan was reviewed and approved before implementation.
Implementation then went bottom-up: contracts → storage → providers → orchestration → rendering →
API → frontend → tests.

**Context and rules supplied.** The assignment brief; a required tech stack (React + FastAPI);
explicit preferences for LangGraph, MCP, clean architecture, async, retries/backoff and logging; and
a standing instruction to distinguish verified from unverified work. Provider choices were settled by
answering the agent's questions rather than by a rules file. No `CLAUDE.md` was used.

**How output was reviewed.** Primarily *empirically*, which is the part that mattered:

- Every claim was checked by running something. Image dimensions measured with Pillow from downloaded
  bytes, video with `ffprobe`, the UI rendered in a real browser via Playwright.
- The live run was the real review. **The two most serious bugs were found by running against Azure,
  not by reading code** — see §6.
- Where the agent's source (Microsoft's docs) conflicted with live behaviour, live behaviour won and
  the registry was corrected with a dated comment.
- Tests were written to pin down behaviour the agent had reasoned about, especially the size
  arithmetic, which is the least obvious correctness property here.

**Honest caveat.** The AI wrote essentially all of the code. The human contribution was direction,
provider access, live-run feedback, and acceptance. Treat the code as reviewed-by-execution rather
than reviewed-line-by-line.

---

## 6. Evidence

### Debugging example: the video was 178 seconds instead of 8

**Symptom.** The first end-to-end fixture run failed at the video stage:

```
render_video FAILED — rendered duration 178.40s outside the required 6-10s
```

**Diagnosis.** The stage caught this itself — the post-render `ffprobe` assertion refused to ship an
out-of-contract asset, which is exactly why that check exists. The cause was my `zoompan` usage:
inputs are `-loop 1 -t <dur>`, so ffmpeg already feeds a *frame sequence*, and `zoompan=d=N` emits N
frames **per input frame**, multiplying clip length by N.

**Fix and verification.** Isolated the filter graph outside the app first, with synthetic frames and
the same beat durations:

```
d=N  →  wrong
d=1  →  ffprobe: width 1080, height 1920, nb_frames 240, duration 8.000000
```

Only then applied it to [rendering/video.py](backend/app/rendering/video.py), with a comment
explaining why `d=1` is load-bearing. Re-ran the full campaign: 28/28 checks passed. The live run
later produced 8.00s at 1080×1920 with real assets.

### Second example: published capability table contradicted the live API

Azure's documentation lists `input_fidelity` as supported on `gpt-image-2.5-sunburst`. The live
deployment returned:

```
400: The model 'gpt-image-2.5-sunburst' does not support the 'input_fidelity' parameter.
```

Two responses: the registry was corrected from live evidence, *and* the adapter now drops a rejected
optional parameter, retries once, and remembers — so a stale capability table can never again fail a
campaign. Pinned by [test_live_error_handling.py](backend/tests/test_live_error_handling.py).

A third, found by the app's own instrumentation: the cost panel reported *"1 call dispatched but never
settled"*. Real leak — `except Exception` does not catch `CancelledError`, so a render cancelled by
its sibling's failure left its ledger row unsettled, i.e. reported as possibly-billed-unknown. Now
settled as `cancelled`.

### Research and documentation links

- [Azure OpenAI image models reference](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/dall-e) — size rules, the GA vs limited-preview split, and that `dall-e-3` retired in March 2026
- [Image generation quickstart](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/dall-e-quickstart?view=foundry-classic) — endpoint shapes and `api-version=2025-04-01-preview`
- [GPT-Image-2.5 Sunburst model card](https://developers.openai.com/api/docs/models/gpt-image-2.5-sunburst) — token-based pricing
- [Tavily search API](https://docs.tavily.com/documentation/api-reference/endpoint/search) — credits, `/search` + `/extract`
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) · [langchain-mcp-adapters](https://github.com/langchain-ai/langchain-mcp-adapters)

### Model and tool usage, with cost

One complete **live** campaign (`c_460f128f27b766be0721`):

| | |
|---|---|
| Provider calls | 13 (10 chat, 3 image) |
| Image generations/edits | 3 — one master scene, two re-frames |
| Research | 2 searches + 3 page fetches, 28.7s |
| **Estimated cost** | **$0.2412** — labelled ESTIMATE |

Image models bill per token, not per image, so any per-image figure would be false precision; where a
provider reports no usage, cost is recorded as *unknown*, not zero. Tavily is counted in credits
because converting to dollars requires knowing the plan. Rates live in
[pricing.json](backend/app/providers/pricing.json) and can be corrected without touching code.

### Unfinished or unverified

- **The `bands` degrade path** produces correct dimensions (unit-tested) but has never run against an
  actual gpt-image-1 deployment.
- **Packshot upload** is tested end-to-end in fixture mode but has not been exercised live, so the
  packshot-anchored `/images/edits` master scene is unproven against Azure.
- **`input_fidelity` is now disabled** for the 2.5 family. Consistency rests entirely on the
  master-scene-as-input mechanism, which is the load-bearing part, but the extra identity hint the
  parameter would have provided is unavailable.
- **Video has no audio**, and uses crossfades plus Ken Burns moves only.
- **Backend comments were stripped** by an external formatting pass late in development; the code is
  unchanged but much of the rationale that explained *why* is gone from the source.
- **Load and concurrency are untested.** Everything was exercised with one campaign at a time.

---

## Layout

```
backend/app/
  domain/     contracts (pure Pydantic, dependency sink)
  prompts/    every prompt as its own .md file
  providers/  adapter protocols, Azure/Tavily/fixture impls, capabilities, pricing
  graph/      LangGraph state, builder, nodes/
  rendering/  deterministic: layout, typography, palette, video (no LLM)
  storage/    models, repositories, artifact store
  worker/     durable job queue + asyncio runner
  security/   untrusted-content sanitisation, upload validation
backend/mcp_servers/   FastMCP research tool server
frontend/              React + Vite + TypeScript + TanStack Query
```

## Time spent

5-6 hours
