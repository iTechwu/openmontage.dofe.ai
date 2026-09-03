"""Server/API tests for Backlot.

These cover the deterministic eval surface in internal/evals/BACKLOT_EVAL_PLAN.md:
API shape, path safety, media/thumb serving, range requests, and loose
performance budgets.
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backlot import server as server_mod
from backlot import state as state_mod


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(state_mod, "PROJECTS_DIR", root)
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", root)
    monkeypatch.setattr(server_mod, "_summary_cache", {})
    monkeypatch.setattr(server_mod, "_PROJECTS_ROOT_STR", __import__("os").path.normcase(str(root.resolve())))
    monkeypatch.setattr(server_mod, "THUMB_CACHE_DIR", tmp_path / "thumbs")
    return root


@pytest.fixture
def client(projects_root, monkeypatch):
    async def no_watch():
        return None

    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    with TestClient(server_mod.create_app()) as c:
        yield c


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_project(root: Path, project_id: str = "film") -> Path:
    project = root / project_id
    (project / "artifacts").mkdir(parents=True)
    (project / "assets" / "images").mkdir(parents=True)
    (project / "assets" / "video").mkdir(parents=True)
    (project / "renders").mkdir(parents=True)
    _write_json(
        project / "project.json",
        {
            "project_id": project_id,
            "title": "Film",
            "pipeline_type": "cinematic",
            "created_at": "2026-07-02T00:00:00Z",
        },
    )
    _write_json(
        project / "checkpoint_script.json",
        {
            "version": "1.0",
            "project_id": project_id,
            "pipeline_type": "cinematic",
            "stage": "script",
            "status": "awaiting_human",
            "timestamp": "2026-07-02T00:01:00Z",
            "artifacts": {},
        },
    )
    return project


def _write_png(path: Path, color: tuple[int, int, int] = (200, 40, 80)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (24, 16), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    path.write_bytes(buf.getvalue())


class TestBacklotServerApi:
    def test_health(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "app": "backlot"}

    def test_public_base_path_is_embedded_in_ui_urls(
        self,
        projects_root,
        monkeypatch,
    ):
        async def no_watch():
            return None

        monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
        monkeypatch.setenv("OPENMONTAGE_BACKLOT_BASE_PATH", "/montage/")

        with TestClient(server_mod.create_app()) as prefixed_client:
            library = prefixed_client.get("/")
            board = prefixed_client.get("/p/film")

        assert library.status_code == 200
        assert '<meta name="backlot-base-path" content="/montage">' in library.text
        assert 'href="/montage/ui/board.css?' in library.text
        assert 'src="/montage/ui/library.js?' in library.text
        assert '<meta name="backlot-base-path" content="/montage">' in board.text
        assert 'src="/montage/ui/board.js?' in board.text

    def test_models_key_authentication_issues_http_only_session(
        self,
        projects_root,
        monkeypatch,
    ):
        async def no_watch():
            return None

        monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
        monkeypatch.setenv("OPENMONTAGE_BACKLOT_BASE_PATH", "/montage")
        monkeypatch.setenv(
            "OPENMONTAGE_BACKLOT_AUTH_URL",
            "http://models.test/internal/mcp/auth-context",
        )
        monkeypatch.setenv("OPENMONTAGE_BACKLOT_SECURE_COOKIE", "false")
        monkeypatch.setattr(
            server_mod,
            "_validate_models_api_key",
            lambda api_key, auth_url: "tenant-a" if api_key == "valid-key" else None,
        )

        with TestClient(server_mod.create_app(), follow_redirects=False) as auth_client:
            page = auth_client.get("/")
            api = auth_client.get("/api/projects")
            rejected = auth_client.post("/auth/session", json={"apiKey": "bad-key"})
            accepted = auth_client.post("/auth/session", json={"apiKey": "valid-key"})
            session_cookie = accepted.headers["set-cookie"].split(";", 1)[0]
            projects = auth_client.get(
                "/api/projects",
                headers={"Cookie": session_cookie},
            )

        assert page.status_code == 303
        assert page.headers["location"] == "/montage/auth"
        assert api.status_code == 401
        assert rejected.status_code == 401
        assert accepted.status_code == 204
        assert "HttpOnly" in accepted.headers["set-cookie"]
        assert "Path=/montage" in accepted.headers["set-cookie"]
        assert "SameSite=none" in accepted.headers["set-cookie"]
        # Fail closed: no workspace-map entry exists, so the session's
        # tenant has no visible projects yet.
        assert projects.status_code == 200
        assert projects.json() == []

    def test_projects_shape_and_state(self, client, projects_root):
        _make_project(projects_root, "film")

        projects = client.get("/api/projects")
        assert projects.status_code == 200
        body = projects.json()
        assert len(body) == 1
        assert body[0]["project_id"] == "film"
        assert body[0]["awaiting_human"] is True
        assert "stage_states" in body[0]

        state = client.get("/api/project/film/state")
        assert state.status_code == 200
        state_body = state.json()
        assert state_body["project_id"] == "film"
        assert state_body["title"] == "Film"
        assert state_body["stages"]

    @pytest.mark.parametrize(
        ("url", "status"),
        [
            ("/api/project/../state", 404),
            ("/api/project/C:/state", 400),
            ("/api/project/nope/state", 404),
        ],
    )
    def test_project_id_rejects_bad_or_unknown_ids(self, client, url, status):
        response = client.get(url)
        assert response.status_code == status

    def test_media_rejects_path_traversal(self, client, projects_root):
        _make_project(projects_root, "film")
        response = client.get("/media/film/%2E%2E/project.json")
        assert response.status_code == 403

    def test_media_serves_range_requests(self, client, projects_root):
        project = _make_project(projects_root, "film")
        media = project / "renders" / "final.mp4"
        media.write_bytes(b"0123456789")

        response = client.get("/media/film/renders/final.mp4", headers={"Range": "bytes=2-5"})

        assert response.status_code == 206
        assert response.content == b"2345"
        assert response.headers["content-range"].startswith("bytes 2-5/10")

    def test_thumb_downscales_image_and_passes_through_non_media(self, client, projects_root):
        project = _make_project(projects_root, "film")
        _write_png(project / "assets" / "images" / "sc1.png")
        text = project / "artifacts" / "note.txt"
        text.write_text("hello", encoding="utf-8")

        image = client.get("/thumb/film/assets/images/sc1.png?w=320")
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/jpeg"
        assert image.content.startswith(b"\xff\xd8")

        passthrough = client.get("/thumb/film/artifacts/note.txt")
        assert passthrough.status_code == 200
        assert passthrough.content == b"hello"


class TestBacklotPerformanceBudgets:
    def test_projects_and_state_stay_within_loose_budgets(self, client, projects_root):
        for i in range(25):
            project = _make_project(projects_root, f"film-{i:02d}")
            _write_json(
                project / "artifacts" / "scene_plan.json",
                {"version": "1.0", "scenes": [{"id": "sc1", "start_seconds": 0, "end_seconds": 1}]},
            )

        t0 = time.perf_counter()
        cold = client.get("/api/projects")
        cold_s = time.perf_counter() - t0
        assert cold.status_code == 200
        assert cold_s < 2.0

        t1 = time.perf_counter()
        warm = client.get("/api/projects")
        warm_s = time.perf_counter() - t1
        assert warm.status_code == 200
        assert warm_s < 0.150

        t2 = time.perf_counter()
        state = client.get("/api/project/film-00/state")
        state_s = time.perf_counter() - t2
        assert state.status_code == 200
        assert state_s < 0.400

    def test_image_thumb_generation_stays_within_budget(self, client, projects_root):
        project = _make_project(projects_root, "film")
        _write_png(project / "assets" / "images" / "sc1.png")

        t0 = time.perf_counter()
        response = client.get("/thumb/film/assets/images/sc1.png?w=640")
        elapsed = time.perf_counter() - t0

        assert response.status_code == 200
        assert elapsed < 1.5


class TestFindingsFixes:
    """Regression tests for dogfood findings F-03 (thumb video fallback)."""

    def test_thumb_never_serves_raw_video_bytes(self, client, projects_root):
        p = _make_project(projects_root, "vid")
        fake_video = p / "renders" / "final.mp4"
        fake_video.parent.mkdir(parents=True, exist_ok=True)
        # Not a real video: ffmpeg poster extraction will fail.
        fake_video.write_bytes(b"\x00" * 4096)
        res = client.get("/thumb/vid/renders/final.mp4")
        assert res.status_code == 404  # never the raw video bytes (F-03)


def _bind_workspace(root, mapping):
    dot = root / ".openmontage"
    dot.mkdir(parents=True, exist_ok=True)
    (dot / "workspace-map.json").write_text(json.dumps(mapping), encoding="utf-8")


class TestBacklotWorkspaceIsolation:
    """docs/0903 §4: an authenticated session only ever sees its own tenant's
    projects. Foreign or unbound projects answer 404 — never 403 — so the id
    space is indistinguishable from "unknown"."""

    @pytest.fixture
    def auth_client(self, projects_root, monkeypatch):
        async def no_watch():
            return None

        monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
        monkeypatch.setenv(
            "OPENMONTAGE_BACKLOT_AUTH_URL",
            "http://models.test/internal/mcp/auth-context",
        )
        monkeypatch.setenv("OPENMONTAGE_BACKLOT_SECURE_COOKIE", "false")
        monkeypatch.setattr(
            server_mod,
            "_validate_models_api_key",
            lambda api_key, auth_url: api_key.removeprefix("key-for-") or None,
        )
        with TestClient(server_mod.create_app(), follow_redirects=False) as instance:
            yield instance

    def _session(self, client, tenant):
        response = client.post("/auth/session", json={"apiKey": f"key-for-{tenant}"})
        assert response.status_code == 204
        return response.headers["set-cookie"].split(";", 1)[0]

    def test_projects_list_scoped_to_session_workspace(
        self, auth_client, projects_root
    ):
        _make_project(projects_root, "film")
        _make_project(projects_root, "promo")
        _bind_workspace(projects_root, {"film": "tenant:tenant-a", "promo": "tenant:tenant-b"})

        own = auth_client.get("/api/projects", headers={"Cookie": self._session(auth_client, "tenant-a")})
        other = auth_client.get("/api/projects", headers={"Cookie": self._session(auth_client, "tenant-b")})

        assert [s["project_id"] for s in own.json()] == ["film"]
        assert [s["project_id"] for s in other.json()] == ["promo"]

    def test_foreign_project_routes_return_404(self, auth_client, projects_root):
        project = _make_project(projects_root, "film")
        _write_png(project / "assets" / "images" / "still.png")
        _bind_workspace(projects_root, {"film": "tenant:tenant-a"})
        cookie = self._session(auth_client, "tenant-b")

        assert auth_client.get("/api/projects", headers={"Cookie": cookie}).json() == []
        assert auth_client.get("/api/project/film/state", headers={"Cookie": cookie}).status_code == 404
        assert auth_client.get("/api/project/film/events", headers={"Cookie": cookie}).status_code == 404
        assert auth_client.get("/p/film", headers={"Cookie": cookie}).status_code == 404
        assert auth_client.get(
            "/thumb/film/assets/images/still.png", headers={"Cookie": cookie}
        ).status_code == 404
        assert auth_client.get(
            "/media/film/assets/images/still.png", headers={"Cookie": cookie}
        ).status_code == 404

    def test_unbound_project_hidden_even_from_own_tenant(
        self, auth_client, projects_root
    ):
        _make_project(projects_root, "orphan")
        cookie = self._session(auth_client, "tenant-a")

        assert auth_client.get("/api/projects", headers={"Cookie": cookie}).json() == []
        assert auth_client.get("/api/project/orphan/state", headers={"Cookie": cookie}).status_code == 404

    def test_workspace_map_updates_are_picked_up(self, auth_client, projects_root):
        _make_project(projects_root, "film")
        cookie = self._session(auth_client, "tenant-a")

        before = auth_client.get("/api/project/film/state", headers={"Cookie": cookie})
        _bind_workspace(projects_root, {"film": "tenant:tenant-a"})
        after = auth_client.get("/api/project/film/state", headers={"Cookie": cookie})

        assert before.status_code == 404
        assert after.status_code == 200

    def test_library_events_never_deliver_foreign_projects(self, projects_root):
        hub = server_mod.ChangeHub()
        workspace_map = server_mod.WorkspaceMap(projects_root)
        _bind_workspace(projects_root, {"film": "tenant:tenant-a"})

        tenant_a = hub.subscribe(
            lambda pid: workspace_map.workspace_of(pid) == "tenant:tenant-a"
        )
        tenant_b = hub.subscribe(
            lambda pid: workspace_map.workspace_of(pid) == "tenant:tenant-b"
        )
        unfiltered = hub.subscribe()

        hub.publish("film")

        assert tenant_a.qsize() == 1
        assert tenant_b.qsize() == 0
        assert unfiltered.qsize() == 1

    def test_auth_disabled_mode_ignores_the_map(self, client, projects_root):
        _make_project(projects_root, "film")
        _bind_workspace(projects_root, {"film": "tenant:tenant-a"})

        response = client.get("/api/projects")

        assert response.status_code == 200
        assert [s["project_id"] for s in response.json()] == ["film"]
