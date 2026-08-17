---
name: recreate-video
description: Analyze a reference video URL or local clip and produce a creatively differentiated new video through OpenMontage. Use when a user asks to recreate, replicate, remix, or make a video like a YouTube Short, TikTok, Douyin, Instagram Reel, or other video link. Supports pasted Douyin share text and direct/short douyin.com links through the OpenMontage CLI or MCP server.
---

# Recreate Video

Turn one reference link into a grounded OpenMontage production. Preserve useful
structure, pacing, and technique; never make a frame-for-frame or deceptive copy.

## Prepare the reference

1. Read `AGENT_GUIDE.md` and `skills/meta/video-reference-analyst.md` completely.
2. Prefer the MCP tool `prepare_reference_clone` when it is available.
3. Otherwise run the CLI from the repository root:

```bash
openmontage clone "<video URL or pasted share text>" \
  --brief "<what the new video should communicate or change>" \
  --json
```

Use `uv run --with-editable . openmontage ...` when the package is not installed.
The command downloads the reference, extracts transcript/scenes/keyframes, runs
the provider preflight, initializes `projects/<project-id>/`, and returns the
first pipeline stage.

For Douyin:

- Accept `www.douyin.com/video/...`, `v.douyin.com/...`, mobile share URLs, and
  pasted Chinese share text containing a URL.
- Use the dedicated cookie-free Douyin downloader (`DouyinShareClient` in
  `tools/analysis/douyin.py`); the downloader routes Douyin to it automatically.
  Never try yt-dlp for Douyin.
- When the public route is restricted, ask for an exported Netscape `cookies.txt`
  and pass `cookie_file` or `--cookies`. Never read browser cookies without
  explicit user authorization.

## Analyze before proposing

Open the returned `video_analysis_brief.json` and inspect the extracted keyframes.
Present the grounded reference summary using all five labels:

- Subject
- Subject Motion
- Scene, with overlays separate from the setting
- Spatial Framing
- Camera

Report motion classification, scene count, pacing, narration, visual language,
and the specific techniques that make the reference effective. Surface failed
analysis steps instead of silently guessing.

## Design a new version

Run the returned capability preflight and present 2-3 differentiated concepts.
Keep only abstract techniques from the reference: hook type, pacing, structural
rhythm, camera vocabulary, or audio architecture. Change the subject, thesis,
story angle, visual treatment, or target platform sufficiently to create an
original production.

Treat model routing as a hard constraint: every LLM, image, video, TTS, music,
or avatar model call must use `provider=dofe` through
the effective DoFe base URL. Fetch its authenticated `GET /v1/models` catalog
first and use only an exact returned ID. Use the `dofe_*` provider tools or
selectors with `DOFE_ENABLED=true`. Never invoke a vendor-direct model tool and never
silently fall back outside the Airouter. If no suitable catalog ID is selected,
stop with a blocker and report which capability needs a model selection.

Before generation, resolve:

- topic and creative difference from the reference;
- target platform, duration, and aspect ratio;
- narration architecture and music source;
- Airouter catalog-model options with itemized cost;
- Remotion versus HyperFrames when both are available;
- templated versus atelier composition mode;
- source-rights confirmation and approval policy.

Wait for explicit approval at every manifest gate. Announce each paid tool,
provider, model, reason, sample/batch scope, and estimated cost before calling it.
Never prescribe a fixed video or STT model in this skill; the current tenant
catalog is the only source of model IDs. HeyGen is not required: it remains
optional only for avatar/presenter/lip-sync workflows and must not be called directly while
AIRouter-only routing is enabled.

Treat a missing or zero tenant-effective price as a hard blocker. The model must
be visible to the API key and have a positive AIRouter rate card before any paid
task is submitted.

## Produce through the pipeline

1. Read `pipeline_defs/<pipeline>.yaml` selected by the preparation result.
2. Run its stages in manifest order.
3. Read each stage director before executing that stage.
4. Read every generation tool's Layer 3 `agent_skills` before writing prompts.
5. Produce the mandatory 10-15 second sample for a reference-driven production.
6. After sample approval, complete the remaining stages and checkpoints.
7. Return `projects/<project-id>/renders/final.mp4` with the final review report.

The final report must include total cost and an itemized breakdown for every
AIRouter task: provider/model, task ID, estimate, final settlement, native
currency, usage/formula, and whether the amount is final. Report download and
local composition separately. Local rendering has no external model fee, but
its infrastructure cost is unknown unless a CPU/RAM/GPU tariff is configured.
Never add CNY and USD into one total without a declared exchange rate and time.

Do not hide a provider failure, silently swap models/runtimes, or replace required
motion with still images. Stop at the relevant approval gate or structured blocker.

## Troubleshoot

- Download failure: for Douyin retry the dedicated `DouyinShareClient` downloader
  (`tools/analysis/douyin.py`); request `cookies.txt` only if the public route is
  restricted. Non-Douyin download failures retry with current yt-dlp.
- Missing transcript: use `dofe_stt` with an STT ID returned by the current
  catalog. If the extracted file has no provider-accessible URL or the selected ID is not visible to the
  tenant, stop and report that AIRouter/storage blocker; do not fall back to a
  direct provider or local Whisper while `DOFE_ENABLED=true`.
- Missing scenes: use uniform frame sampling and disclose reduced confidence.
- MCP unavailable: use the same CLI command; the output contract is identical.
- Docker: use `docker compose run --rm openmontage-cli clone ... --json` or connect
  an MCP client to `http://localhost:8765/mcp`.
