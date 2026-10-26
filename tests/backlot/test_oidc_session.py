from __future__ import annotations

import hashlib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backlot.oidc_routes import COOKIE, SUPPRESS_COOKIE, install_oidc
from backlot.oidc_session import (AuthenticationExpired, AuthenticationUnavailable, CookieRenewalRequired,
                                 OidcConfig, OidcSessions, token_response)
from backlot.session_vault import VaultUnavailable


def tokens(generation=0, context="native"):
    return token_response({"session_version": "2", "session_context": context, "expires_in": 300,
                           "access_token": "at-" + str(generation), "refresh_token": "rt-" + str(generation),
                           "id_token": "id-token", "session_id": "sid", "family_id": "family",
                           "refresh_generation": generation, "auth_epoch": 1,
                           "idle_expires_at": int(time.time() * 1000) + 2592000000,
                           "refresh_expires_at": int(time.time() * 1000) + 2592000000})


class IdentityClient:
    def __init__(self):
        self.generation = 0
        self.operations = {}
        self.confirmed = set()
        self.calls = []
        self.context = "native"
        self.lost = False
        self.active = True
        self.logout_confirmed = False

    def verify_id_token(self, _tokens, nonce=None):
        return {"sub": "user"}

    def identity(self, _tokens):
        if not self.active:
            raise AuthenticationExpired()
        return {"sub": "user", "workspace": "tenant:verified", "sso_tenant_id": "verified", "sso_team_id": "team"}

    def token(self, body):
        op = body["operation_id"]
        self.calls.append(("rotate", dict(body)))
        if op not in self.operations:
            assert body["refresh_token"] == "rt-" + str(self.generation)
            self.generation += 1
            self.operations[op] = tokens(self.generation, self.context)
        if self.lost:
            self.lost = False
            raise AuthenticationUnavailable("response lost after commit")
        return self.operations[op]

    def operation(self, path, body):
        self.calls.append((path, dict(body)))
        if path.endswith("logout-confirmation"):
            return {"confirmed": self.logout_confirmed}
        if path.endswith("/logout"):
            return {"revoked": True, "logout_ticket": "opaque-logout-ticket-for-test"}
        if path.endswith("bootstrap-exchange"):
            return tokens(context=self.context)
        if path.endswith("confirmation"):
            return {"active": self.active, "confirmed": body["operation_id"] in self.confirmed}
        if path.endswith("renew-ticket"):
            return {"ticket": "opaque-ticket-for-" + body["operation_id"]}
        return {"ok": True}


@pytest.fixture
def manager(tmp_path):
    config = OidcConfig("https://identity.example/api", "montage-rp", "unit-client", "https://montage.example",
                        tmp_path / "auth" / "sessions.db", Fernet.generate_key().decode(),
                        "https://models.example/api/internal/sso/workspace-context", "unit-internal")
    client = IdentityClient()
    service = OidcSessions(config, client)
    cookie, _ = service.create(tokens())
    return service, client, cookie


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def test_native_rotates_every_accepted_business_request_and_persists_before_dispatch(manager):
    service, client, cookie = manager
    for index in range(3):
        session = service.prepare(cookie, digest("business poll " + str(index)))
        assert service.vault.get(cookie)["tokens"]["refresh_token"] == session["tokens"]["refresh_token"] == "rt-" + str(index + 1)
    assert not any("renew-ticket" in path for path, _ in client.calls)


def test_lost_rotation_response_survives_restart_and_recovers_exact_prior_operation(manager):
    service, client, cookie = manager
    client.lost = True
    with pytest.raises(AuthenticationUnavailable):
        service.prepare(cookie, digest("lost request"))
    original = service.vault.get(cookie)["request_pending"]
    restarted = OidcSessions(service.config, client)
    restarted.prepare(cookie, digest("new logical request"))
    calls = [body for path, body in client.calls if path == "rotate"]
    assert calls[0]["operation_id"] == calls[1]["operation_id"] == original["operation_id"]
    assert calls[0]["request_digest"] == calls[1]["request_digest"]
    assert calls[2]["operation_id"] != calls[1]["operation_id"]
    assert client.generation == 2


def test_separate_workers_share_one_refresh_writer(manager):
    service, client, cookie = manager
    def request(index):
        return OidcSessions(service.config, client).prepare(cookie, digest(str(index)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(request, range(20)))
    assert client.generation == 20
    assert service.vault.get(cookie)["tokens"]["refresh_generation"] == 20


def test_browser_cookie_confirmation_is_mandatory_and_does_not_rotate_again_on_retry(manager):
    service, client, cookie = manager
    client.context = "browser"
    value = service.vault.get(cookie)
    value["tokens"]["session_context"] = "browser"
    service.save(cookie, value)
    operation = str(uuid.uuid4())
    with pytest.raises(CookieRenewalRequired) as gate:
        service.prepare(cookie, digest("read"), operation)
    assert gate.value.operation_id == operation
    assert client.generation == 1
    client.confirmed.add(operation)
    service.prepare(cookie, digest("read"), operation)
    assert client.generation == 1
    assert "request_pending" not in service.vault.get(cookie)


def test_logout_suppression_survives_network_error_and_blocks_business(manager):
    service, client, cookie = manager
    def unavailable(*_):
        raise AuthenticationUnavailable()
    client.operation = unavailable
    with pytest.raises(AuthenticationUnavailable):
        service.logout(cookie, "device")
    with pytest.raises(AuthenticationExpired):
        OidcSessions(service.config, client).prepare(cookie, digest("later"))


def test_backchannel_tombstone_prevents_inflight_refresh_resurrection(manager):
    service, _, cookie = manager
    old = service.vault.get(cookie)
    service.vault.revoke_sid("sid")
    with pytest.raises(VaultUnavailable):
        service.save(cookie, old)


def test_bootstrap_replay_reuses_one_opaque_cookie_and_independent_target_family(manager):
    service, client, _ = manager
    client.generation = 0
    # Different family from the fixture session.
    previous = client.operation
    def exchange(path, body):
        result = previous(path, body)
        if path.endswith("logout-confirmation"):
            return {"confirmed": self.logout_confirmed}
        if path.endswith("/logout"):
            return {"revoked": True, "logout_ticket": "opaque-logout-ticket-for-test"}
        if path.endswith("bootstrap-exchange"):
            result["family_id"] = "target-family"
        return result
    client.operation = exchange
    cookie, _ = service.bootstrap("one-use-opaque-ticket")
    again, _ = OidcSessions(service.config, client).bootstrap("one-use-opaque-ticket")
    assert cookie == again
    assert sum(path.endswith("bootstrap-exchange") for path, _ in client.calls) == 1


def app_client(manager):
    service, client, cookie = manager
    app = FastAPI()
    @app.get("/api/library")
    async def library(request: Request):
        return {"workspace": request.state.oidc_workspace}
    install_oidc(app, sessions=service)
    return TestClient(app, base_url=service.config.public_origin), client, cookie


def test_http_business_polls_rotate_static_status_and_sse_do_not(manager):
    http, identity, cookie = app_client(manager)
    http.cookies.set(COOKIE, cookie)
    assert http.get("/api/library").json() == {"workspace": "tenant:verified"}
    assert http.get("/api/library").status_code == 200
    assert identity.generation == 2
    renewed = http.get("/api/library")
    cookie_header = renewed.headers["set-cookie"]
    assert "Max-Age=" in cookie_header and "Secure" in cookie_header and "HttpOnly" in cookie_header
    http.get("/auth/session-worker.js")
    assert identity.generation == 3


def test_http_logout_rejects_csrf_before_changing_any_session(manager):
    http, identity, cookie = app_client(manager)
    http.cookies.set(COOKIE, cookie)
    response = http.post("/auth/logout", json={}, headers={"origin": "https://attacker.example"})
    assert response.status_code == 403
    assert SUPPRESS_COOKIE not in response.cookies
    assert not manager[0].vault.get(cookie).get("logout_pending")
    response = http.post("/auth/logout", json={"scope": "all"}, headers={"origin": "https://montage.example"})
    assert response.status_code == 200
    assert SUPPRESS_COOKIE in response.cookies
    assert all("Secure" in header for header in response.headers.get_list("set-cookie") if "__Host-" in header)
    assert manager[0].vault.get(cookie) is None


def test_http_browser_gate_returns_json_to_worker_even_for_navigation(manager):
    http, identity, cookie = app_client(manager)
    identity.context = "browser"
    session = manager[0].vault.get(cookie)
    session["tokens"]["session_context"] = "browser"
    manager[0].save(cookie, session)
    http.cookies.set(COOKIE, cookie)
    op = str(uuid.uuid4())
    response = http.get("/api/library", headers={"accept": "text/html", "x-dofe-operation-id": op})
    assert response.status_code == 409
    assert response.json()["error"] == "COOKIE_RENEWAL_REQUIRED"
    identity.confirmed.add(op)
    assert http.get("/api/library", headers={"x-dofe-operation-id": op}).status_code == 200
    assert identity.generation == 1


def test_cross_origin_subresource_cannot_extend_native_session(manager):
    http, identity, cookie = app_client(manager)
    http.cookies.set(COOKIE, cookie)
    assert http.get('/api/library', headers={'sec-fetch-site':'same-site','sec-fetch-mode':'no-cors'}).status_code == 401
    assert identity.generation == 0


def test_backlot_oidc_gate_sets_workspace_before_legacy_guard(manager, monkeypatch, tmp_path):
    from backlot import server, state
    from backlot.oidc_session import OidcConfig
    import backlot.oidc_routes as routes
    monkeypatch.setenv('OPENMONTAGE_OIDC_ENABLED', 'true')
    monkeypatch.setenv('OPENMONTAGE_BACKLOT_BASE_PATH', '')
    monkeypatch.setenv('OPENMONTAGE_BACKLOT_AUTH_URL', 'https://models.example/legacy')
    monkeypatch.setattr(OidcConfig, 'from_environment', classmethod(lambda cls: manager[0].config))
    monkeypatch.setattr(routes, 'OidcSessions', lambda *_: manager[0])
    root = tmp_path / 'projects'
    root.mkdir()
    monkeypatch.setattr(server, 'PROJECTS_DIR', root)
    monkeypatch.setattr(state, 'PROJECTS_DIR', root)
    async def no_watch(): pass
    monkeypatch.setattr(server, '_watch_projects', no_watch)
    with TestClient(server.create_app(), base_url='https://montage.example') as http:
        http.cookies.set(COOKIE, manager[2])
        response = http.get('/api/projects')
        assert response.status_code == 200
        assert manager[1].generation == 1
        # The default login page is the RP, while historical embed stays separate.
        assert '登录 OpenMontage' in http.get('/auth').text
        assert http.post('/auth/session', json={'apiKey':'not-accepted'}).status_code == 403


def test_browser_slow_confirmation_recovers_expired_access_without_losing_valid_refresh(manager):
    service, client, cookie = manager
    client.context = 'browser'
    session = service.vault.get(cookie)
    session['tokens']['session_context'] = 'browser'
    service.save(cookie, session)
    operation = str(uuid.uuid4())
    with pytest.raises(CookieRenewalRequired):
        service.prepare(cookie, digest('read'), operation)
    session = service.vault.get(cookie)
    session['tokens']['access_expires_at'] = 1
    service.save(cookie, session)
    with pytest.raises(CookieRenewalRequired) as second:
        service.prepare(cookie, digest('read'), operation)
    assert second.value.operation_id != operation
    client.confirmed.add(second.value.operation_id)
    service.prepare(cookie, digest('read'), operation)
    assert client.generation == 2
    assert service.vault.get(cookie)['completed'] == [operation]


def test_authorization_explicitly_requests_session_v2(manager):
    from urllib.parse import parse_qs, urlparse
    service, client, _ = manager
    client.discovery = lambda: {'authorization_endpoint':'https://identity.example/api/oauth/authorize'}
    state, url = service.authorization('https://montage.example/auth/callback', '/', False)
    query = parse_qs(urlparse(url).query)
    assert query['session_version'] == ['2']
    assert query['code_challenge_method'] == ['S256']
    assert service.vault.get('transaction:' + state)['nonce'] == query['nonce'][0]


def test_logout_lost_response_recovers_with_original_expired_access_after_restart(manager):
    service, client, cookie = manager
    calls = []
    def logout(path, body):
        assert path.endswith('/logout')
        calls.append(dict(body))
        if len(calls) == 1:
            raise AuthenticationUnavailable('committed response lost')
        assert body == calls[0]
        return {'revoked':True,'logout_ticket':'original-server-ticket'}
    client.operation = logout
    with pytest.raises(AuthenticationUnavailable):
        service.logout(cookie, 'all')
    value = service.vault.get(cookie)
    value['logout_pending']['access_expires_at'] = 1
    value['tokens']['access_expires_at'] = 1
    service.save(cookie, value)
    restarted = OidcSessions(service.config, client)
    assert restarted.logout(cookie, 'all')['loggedOut'] is True
    assert not client.operations  # never refresh a revoked RT before recovering logout
    assert len(calls) == 2


def test_browser_logout_pending_survives_backchannel_until_actual_central_confirmation(manager):
    service, client, cookie = manager
    value = service.vault.get(cookie)
    value['tokens']['session_context'] = client.context = 'browser'
    service.save(cookie, value)
    result = service.logout(cookie, 'device')
    assert result['logout_ticket']
    service.vault.revoke_sid('sid')
    assert service.vault.get(cookie)['logout_pending']
    with pytest.raises(AuthenticationUnavailable):
        OidcSessions(service.config, client).finish_logout(cookie)
    client.logout_confirmed = True
    OidcSessions(service.config, client).finish_logout(cookie)
    assert service.vault.get(cookie) is None


def test_http_unconfirmed_logout_keeps_persistent_opaque_recovery_cookie(manager):
    http, client, cookie = app_client(manager)
    http.cookies.set(COOKIE, cookie)
    def unavailable(*_):
        raise AuthenticationUnavailable()
    client.operation = unavailable
    response = http.post("/auth/logout", json={}, headers={"origin": "https://montage.example"})
    assert response.status_code == 503
    header = next(item for item in response.headers.get_list("set-cookie") if item.startswith(COOKIE + "="))
    assert "Max-Age=2592000" in header and "HttpOnly" in header and "Secure" in header
    assert manager[0].vault.get(cookie)["logout_pending"]["access_token"]
    assert http.get("/api/library").status_code == 401
    assert http.get("/auth/status").json()["logoutPending"] is True


@pytest.mark.parametrize("context", ["native", "browser"])
def test_long_offline_rotation_recovery_persists_rt_then_uses_a_distinct_op_before_dispatch(manager, monkeypatch, context):
    service, client, cookie = manager
    client.context = context
    current = service.vault.get(cookie)
    current["tokens"]["session_context"] = context
    service.save(cookie, current)
    client.lost = True
    with pytest.raises(AuthenticationUnavailable):
        service.prepare(cookie, digest("accepted user request"), str(uuid.uuid4()))
    pending = service.vault.get(cookie)["request_pending"]
    old_op = pending["operation_id"]
    saved = client.operations[old_op]
    client.operations[old_op] = token_response({**saved, "expires_in": 0})
    future = time.time() + 2 * 86400
    monkeypatch.setattr(time, "time", lambda: future)
    original = client.token
    resumed = []
    def token(body):
        resumed.append(body["operation_id"])
        if len(resumed) == 2:
            assert service.vault.get(cookie)["tokens"]["refresh_token"] == "rt-1"
        return original(body)
    client.token = token
    restarted = OidcSessions(service.config, client)
    if context == "browser":
        with pytest.raises(CookieRenewalRequired) as gate:
            restarted.prepare(cookie, digest("accepted user request"), old_op)
        assert gate.value.operation_id != old_op
        client.confirmed.add(gate.value.operation_id)
    result = restarted.prepare(cookie, digest("accepted user request"), old_op)
    assert result["tokens"]["access_token"] == "at-2"
    assert result["tokens"]["access_expires_at"] > future * 1000
    assert resumed[0] == old_op and resumed[1] != old_op
    assert result["completed"] == [old_op]
    assert "request_pending" not in result
