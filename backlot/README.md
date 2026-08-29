# Backlot — the living storyboard

A read-only local board that shows a production happening: pipeline stages
lighting up, the script as a screenplay page, the scene plan as a filmstrip
that fills in as assets generate, decisions, spend, and activity — all
derived from what the pipeline already writes to `projects/<id>/`.

```bash
python -m backlot open <project-id>   # start server if needed + open browser
python -m backlot open                # library view (all projects)
python -m backlot serve --port 4750   # run the server in the foreground
```

## Public read-only deployment

`docker compose up -d openmontage-backlot` starts a least-privilege board
service on host loopback port `14750`. The container mounts `projects/`
read-only and does not receive the OpenMontage model/provider credentials.

The public deployment uses these settings:

| Variable | Default | Purpose |
|---|---|---|
| `OPENMONTAGE_BACKLOT_BASE_PATH` | `/montage` | Browser-visible path prefix |
| `OPENMONTAGE_BACKLOT_AUTH_URL` | Models internal `auth-context` | Validates the submitted Models API key |
| `OPENMONTAGE_BACKLOT_PORT` | `14750` | CI loopback port for the tunnel |

The login exchanges a valid Models API key for a random, 12-hour, Secure,
HttpOnly and `SameSite=None` session cookie so Yootun's cross-site sandboxed
Web View can retain the read-only session. The key is not stored in the URL,
browser storage, or Backlot session state. Local `python -m backlot serve`
remains unauthenticated unless `OPENMONTAGE_BACKLOT_AUTH_URL` is explicitly set.

The reverse proxy must strip `/montage` before forwarding to Backlot. Disable
proxy buffering for `/montage/api/*/events` so the live SSE updates are not
delayed.

## How it stays live

No agent involvement. A `watchfiles` watcher on `projects/` publishes change
notifications over SSE; the browser refetches board state. State sources:

| Board element | Disk source |
|---|---|
| identity / rail order | `project.json` + `pipeline_defs/<type>.yaml` |
| stage states, gates, versions | `checkpoint_<stage>.json` + `history/` |
| script card / modal | `artifacts/script.json` |
| filmstrip cards | `scene_plan × script × asset_manifest` join |
| generating shimmer, activity | `events.jsonl` (written by `BaseTool` instrumentation) |
| cost meter | checkpoint `cost_snapshot` |
| renders | `renders/*.mp4` (+ root-level mp4 heuristic) |

Projects without checkpoints degrade gracefully to a "what the watcher
found" view — media, snapshots, renders.

**Replay**: a completed run can be scrubbed end-to-end (▶ REPLAY RUN on the
board) — reconstructed from checkpoint history and event timestamps.

Try it without a real production:

```bash
python scripts/backlot_simulate_run.py          # live demo run (~1 min)
python -m backlot open backlot-demo-run
```

Design doc: `internal/design/LIVING_STORYBOARD.md`.
