"""Backlot server — FastAPI app: board state API, SSE change feed, media.

The watcher observes ``projects/`` with watchfiles; on any change it bumps a
per-project version and wakes SSE subscribers, who tell the browser to
refetch state. The server never writes to project directories.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import secrets
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from backlot.state import PROJECTS_DIR, REPO_ROOT, list_projects, load_board_state, summarize_project
from backlot.workspace_map import WorkspaceMap

UI_DIR = Path(__file__).resolve().parent / "ui"
THUMB_CACHE_DIR = Path(
    os.environ.get("OPENMONTAGE_BACKLOT_CACHE_DIR", REPO_ROOT / ".backlot" / "thumbs")
)
THUMB_WIDTHS = (320, 640, 960)

# Paths inside a project whose changes are pure noise for the board.
_IGNORE_PARTS = {"node_modules", ".git", "__pycache__", ".cache"}

SSE_HEARTBEAT_SECONDS = 15


def _base_path() -> str:
    value = os.environ.get("OPENMONTAGE_BACKLOT_BASE_PATH", "").strip()
    if not value or value == "/":
        return ""
    if not value.startswith("/") or ".." in value or "//" in value:
        raise RuntimeError("OPENMONTAGE_BACKLOT_BASE_PATH must be one safe absolute path")
    return value.rstrip("/")


def _ui_html(name: str, assets: tuple[str, ...], base_path: str) -> HTMLResponse:
    content = (UI_DIR / name).read_text(encoding="utf-8")
    content = content.replace(
        "<head>",
        f'<head>\n<meta name="backlot-base-path" content="{html.escape(base_path, quote=True)}">',
        1,
    )
    for asset in assets:
        path = UI_DIR / asset
        if path.is_file():
            version = str(int(path.stat().st_mtime))
            content = content.replace(
                f"/ui/{asset}",
                f"{base_path}/ui/{asset}?v={version}",
            )
    return HTMLResponse(content)


class ModelsAuthUnavailable(RuntimeError):
    """Raised when the internal Models authentication service is unavailable."""


def _validate_models_api_key(api_key: str, auth_url: str) -> str | None:
    import requests

    try:
        response = requests.get(
            auth_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        raise ModelsAuthUnavailable("Models authentication is unavailable") from exc
    if response.status_code == 200:
        return response.headers.get("X-Dofe-Tenant-Id", "unknown").strip() or "unknown"
    if response.status_code in {401, 403}:
        return None
    raise ModelsAuthUnavailable("Models authentication returned an unexpected response")


def _auth_html(base_path: str) -> HTMLResponse:
    template = (UI_DIR / "auth.html").read_text(encoding="utf-8")
    return HTMLResponse(
        template.replace("__BACKLOT_BASE_PATH_JSON__", json.dumps(base_path))
    )


class ChangeHub:
    """Fan-out of project-change notifications to SSE subscribers.

    Subscriptions carry an ``accepts`` predicate: a board subscribed to one
    project only ever receives that project's ids, and an authenticated
    subscriber only ever receives ids inside its workspace, so unrelated or
    foreign-project bursts can't flood its queue and starve out the one
    notification it actually needs.
    """

    def __init__(self) -> None:
        self._subscribers: dict[asyncio.Queue, Callable[[str], bool]] = {}

    def subscribe(self, accepts: Optional[Callable[[str], bool]] = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers[q] = accepts or (lambda _project_id: True)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.pop(q, None)

    def publish(self, project_id: str) -> None:
        for q, accepts in list(self._subscribers.items()):
            if not accepts(project_id):
                continue
            try:
                q.put_nowait(project_id)
            except asyncio.QueueFull:
                # Queue holds only THIS subscriber's relevant ids, so a full
                # queue already guarantees a pending wake-up → safe to drop.
                pass


hub = ChangeHub()

# Library summaries are expensive to derive (full state parse per project);
# cache per project and invalidate from the watcher.
_summary_cache: dict[str, dict] = {}


def _invalidate_summary(project_id: str) -> None:
    _summary_cache.pop(project_id, None)


def _cached_summaries() -> list[dict]:
    if not PROJECTS_DIR.is_dir():
        return []
    summaries = []
    for entry in sorted(PROJECTS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith(("_", ".")):
            continue
        cached = _summary_cache.get(entry.name)
        if cached is None:
            try:
                cached = summarize_project(entry)
            except Exception:
                cached = {
                    "project_id": entry.name, "title": entry.name,
                    "pipeline_type": "unknown", "has_pipeline_state": False,
                    "poster": None, "live": False, "last_activity": 0,
                    "active_stage": None, "awaiting_human": False,
                    "stage_states": [], "completed_count": 0,
                    "render_count": 0, "scene_count": 0, "error": "unreadable",
                }
            _summary_cache[entry.name] = cached
        summaries.append(cached)
    summaries.sort(key=lambda s: (not s["live"], -(s["last_activity"] or 0)))
    return summaries


# Watch-loop hot path: pure string comparison, no per-path filesystem calls
# (change batches can be thousands of paths during a render).
import os as _os

_PROJECTS_ROOT_STR = _os.path.normcase(str(PROJECTS_DIR.resolve()))


def _project_of_change(path_str: str) -> Optional[str]:
    """Map a changed filesystem path to a project id (None = irrelevant)."""
    norm = _os.path.normcase(_os.path.normpath(path_str))
    if not norm.startswith(_PROJECTS_ROOT_STR):
        return None
    rel = norm[len(_PROJECTS_ROOT_STR):].lstrip("\\/")
    if not rel:
        return None
    parts = rel.replace("\\", "/").split("/")
    if _IGNORE_PARTS.intersection(parts):
        return None
    return parts[0]


async def _watch_projects() -> None:
    """Background task: watch projects/ and publish debounced changes."""
    try:
        from watchfiles import awatch
    except ImportError:
        return  # watcher unavailable → board still works via manual refresh
    if not PROJECTS_DIR.is_dir():
        return
    async for changes in awatch(PROJECTS_DIR, recursive=True, step=400):
        touched: set[str] = set()
        for _change, path_str in changes:
            pid = _project_of_change(path_str)
            if pid:
                touched.add(pid)
        for pid in touched:
            _invalidate_summary(pid)
            hub.publish(pid)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Own and cleanly stop the project watcher with FastAPI's lifespan API."""

    task = asyncio.create_task(_watch_projects())
    app.state.watch_task = task
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    app = FastAPI(title="Backlot", docs_url=None, redoc_url=None, lifespan=_lifespan)
    base_path = _base_path()
    auth_url = os.environ.get("OPENMONTAGE_BACKLOT_AUTH_URL", "").strip()
    secure_cookie = os.environ.get(
        "OPENMONTAGE_BACKLOT_SECURE_COOKIE", "true"
    ).lower() not in {"0", "false", "no"}
    session_ttl = 12 * 60 * 60
    sessions: dict[str, tuple[float, str]] = {}
    workspace_map = WorkspaceMap(PROJECTS_DIR)

    def session_is_valid(token: str | None) -> bool:
        if not token:
            return False
        now = time.monotonic()
        expires_at = sessions.get(token, (0, ""))[0]
        if expires_at <= now:
            sessions.pop(token, None)
            return False
        return True

    def session_workspace(token: str | None) -> str | None:
        entry = sessions.get(token or "")
        if entry and entry[0] > time.monotonic():
            return entry[1]
        return None

    def _request_workspace(request: Request) -> str | None:
        """Session workspace in auth mode; None means dev mode (no scoping)."""
        if not auth_url:
            return None
        return session_workspace(request.cookies.get("openmontage_backlot_session"))

    def authorize_project(request: Request, project_id: str) -> None:
        """404 unless the session workspace owns the project (auth mode only).

        Fails closed as 404 (not 403) so a foreign project id is
        indistinguishable from an unknown one.
        """
        if not auth_url:
            return
        workspace = session_workspace(
            request.cookies.get("openmontage_backlot_session")
        )
        if workspace is None or workspace_map.workspace_of(project_id) != workspace:
            raise HTTPException(status_code=404, detail="unknown project")

    def _workspace_accepts(workspace: str | None) -> Callable[[str], bool]:
        """SSE predicate: dev mode sees all changes; auth mode sees only the
        session workspace's projects (nothing at all without a workspace)."""
        if not auth_url:
            return lambda _project_id: True
        if workspace is None:
            return lambda _project_id: False
        return lambda project_id: workspace_map.workspace_of(project_id) == workspace

    if auth_url:
        @app.middleware("http")
        async def require_models_session(request: Request, call_next):
            path = request.url.path
            if (
                path == "/api/health"
                or path == "/auth"
                or path == "/auth/session"
                or session_is_valid(request.cookies.get("openmontage_backlot_session"))
            ):
                return await call_next(request)
            if path == "/" or path.startswith("/p/"):
                return RedirectResponse(f"{base_path}/auth", status_code=303)
            return JSONResponse(
                {"error": {"code": "OPENMONTAGE_BACKLOT_UNAUTHORIZED"}},
                status_code=401,
            )

        @app.get("/auth")
        async def auth_page() -> HTMLResponse:
            return _auth_html(base_path)

        @app.post("/auth/session")
        async def create_auth_session(request: Request) -> Response:
            try:
                payload = await request.json()
            except (json.JSONDecodeError, ValueError):
                payload = {}
            authorization = request.headers.get("authorization", "")
            api_key = authorization.removeprefix("Bearer ").strip()
            if not api_key:
                api_key = payload.get("apiKey") if isinstance(payload, dict) else None
            if not isinstance(api_key, str) or not api_key or len(api_key) > 4096:
                return JSONResponse({"error": "invalid_api_key"}, status_code=401)
            try:
                tenant_id = await asyncio.to_thread(
                    _validate_models_api_key, api_key, auth_url
                )
            except ModelsAuthUnavailable:
                return JSONResponse({"error": "auth_unavailable"}, status_code=503)
            if not tenant_id:
                return JSONResponse({"error": "invalid_api_key"}, status_code=401)
            now = time.monotonic()
            for expired_token, expires_at in list(sessions.items()):
                if expires_at[0] <= now:
                    sessions.pop(expired_token, None)
            token = secrets.token_urlsafe(32)
            # Canonical workspace id, matching the JobService attribution
            # (workspace_id = tenant:{tenantId}) so board visibility lines up
            # with job ownership in workspace-map.json.
            sessions[token] = (now + session_ttl, f"tenant:{tenant_id}")
            response = Response(status_code=204)
            response.set_cookie(
                "openmontage_backlot_session",
                token,
                max_age=session_ttl,
                httponly=True,
                secure=secure_cookie,
                # Backlot is intentionally embedded by Yootun's sandboxed Web View.
                # Secure + SameSite=None lets that cross-site iframe retain the
                # read-only session without exposing the Models API key.
                samesite="none",
                path=base_path or "/",
            )
            return response

    # ---- API ----------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "app": "backlot"}

    @app.get("/api/projects")
    async def projects(request: Request) -> list:
        summaries = await asyncio.to_thread(_cached_summaries)
        workspace = _request_workspace(request)
        if workspace is None:
            return summaries
        allowed = workspace_map.projects_for(workspace)
        return [s for s in summaries if s["project_id"] in allowed]

    @app.get("/api/project/{project_id}/state")
    async def project_state(project_id: str, request: Request) -> dict:
        authorize_project(request, project_id)
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(load_board_state, project_dir)

    @app.get("/api/project/{project_id}/events")
    async def project_events(project_id: str, request: Request) -> StreamingResponse:
        authorize_project(request, project_id)
        accepts = _workspace_accepts(_request_workspace(request))
        _safe_project_dir(project_id)  # 404 early for unknown projects

        async def stream():
            q = hub.subscribe(lambda pid: pid == project_id and accepts(pid))
            try:
                yield _sse({"type": "hello", "project_id": project_id})
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        await asyncio.wait_for(q.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        yield _sse({"type": "heartbeat", "ts": time.time()})
                        continue
                    # Coalesce bursts: drain anything else queued.
                    while not q.empty():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    yield _sse({"type": "change", "project_id": project_id})
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    @app.get("/api/library/events")
    async def library_events(request: Request) -> StreamingResponse:
        accepts = _workspace_accepts(_request_workspace(request))

        async def stream():
            q = hub.subscribe(accepts)
            try:
                yield _sse({"type": "hello"})
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        changed = await asyncio.wait_for(q.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        yield _sse({"type": "heartbeat", "ts": time.time()})
                        continue
                    while not q.empty():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    yield _sse({"type": "change", "project_id": changed})
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    # ---- Thumbnails (downscaled, cached on disk) ------------------------

    @app.get("/thumb/{project_id}/{file_path:path}")
    async def thumb(
        project_id: str, file_path: str, request: Request, w: int = 640
    ) -> FileResponse:
        authorize_project(request, project_id)
        project_dir = _safe_project_dir(project_id)
        target = (project_dir / file_path).resolve()
        try:
            target.relative_to(project_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="path escapes project")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="media not found")
        width = min(THUMB_WIDTHS, key=lambda x: abs(x - w))
        cached = await asyncio.to_thread(_thumbnail_for, target, width)
        if cached is None:
            # Never fall back to raw video bytes for an <img> consumer (F-03);
            # non-thumbable images are safe to serve as-is.
            if target.suffix.lower() in {".mp4", ".webm", ".mov"}:
                raise HTTPException(status_code=404, detail="no poster frame available")
            return FileResponse(target)
        return FileResponse(cached, media_type="image/jpeg")

    # ---- Media (range requests handled by FileResponse) ---------------

    @app.get("/media/{project_id}/{file_path:path}")
    async def media(
        project_id: str, file_path: str, request: Request
    ) -> FileResponse:
        authorize_project(request, project_id)
        project_dir = _safe_project_dir(project_id)
        target = (project_dir / file_path).resolve()
        try:
            target.relative_to(project_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="path escapes project")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="media not found")
        return FileResponse(target)

    # ---- UI ------------------------------------------------------------

    @app.get("/p/{project_id}")
    async def board_page(project_id: str, request: Request) -> HTMLResponse:
        authorize_project(request, project_id)
        return _ui_html("board.html", ("board.css", "board.js"), base_path)

    @app.get("/p/{project_path:path}")
    async def board_page_path(project_path: str, request: Request) -> HTMLResponse:
        authorize_project(request, project_path.split("/", 1)[0])
        return _ui_html("board.html", ("board.css", "board.js"), base_path)

    @app.get("/")
    async def library_page() -> HTMLResponse:
        return _ui_html("index.html", ("board.css", "library.js"), base_path)

    if UI_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")

    # The board is a long-lived SPA: a tab keeps running whatever board.js it
    # loaded, and browsers heuristically cache /ui assets. no-cache forces a
    # conditional revalidation (cheap 304 via ETag) on every load so UI fixes
    # show up on a plain refresh. Media/thumb responses keep normal caching.
    @app.middleware("http")
    async def ui_no_cache(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if (
            path == "/"
            or path == "/auth"
            or path.startswith("/ui")
            or path.startswith("/p/")
        ):
            response.headers["Cache-Control"] = "no-cache"
        return response

    return app


def _safe_project_dir(project_id: str) -> Path:
    # ':' rejects Windows drive-relative ids like "C:" (PROJECTS_DIR / "C:"
    # collapses back to PROJECTS_DIR itself).
    if any(c in project_id for c in "/\\:") or project_id in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid project id")
    project_dir = PROJECTS_DIR / project_id
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"unknown project: {project_id}")
    return project_dir


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _thumbnail_for(source: Path, width: int) -> Optional[Path]:
    """Downscale an image (or extract a video poster frame) to a cached JPEG."""
    suffix = source.suffix.lower()
    is_image = suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    is_video = suffix in {".mp4", ".webm", ".mov"}
    if not (is_image or is_video):
        return None
    try:
        import hashlib
        stat = source.stat()
        key = hashlib.sha1(
            f"{source}|{stat.st_mtime_ns}|{stat.st_size}|{width}".encode()
        ).hexdigest()[:20]
        cached = THUMB_CACHE_DIR / f"{key}.jpg"
        if cached.is_file():
            return cached
        THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Unique temp per request — concurrent misses for the same source
        # must not write (and replace from) the same temp file.
        import uuid
        tmp = THUMB_CACHE_DIR / f"{key}.{uuid.uuid4().hex[:8]}.tmp.jpg"
        if is_video:
            import subprocess
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", "1.5",
                 "-i", str(source), "-frames:v", "1",
                 "-vf", f"scale={width}:-2", str(tmp)],
                capture_output=True, timeout=30,
            )
            if result.returncode != 0 or not tmp.is_file():
                return None
        else:
            from PIL import Image
            with Image.open(source) as img:
                img = img.convert("RGB")
                img.thumbnail((width, width * 3))
                img.save(tmp, "JPEG", quality=82)
        tmp.replace(cached)
        return cached
    except Exception:
        return None


app = create_app()
