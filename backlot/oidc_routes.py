"""Top-level OIDC RP. The browser receives opaque cookies and one-use tickets only."""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, unquote, urlsplit

import jwt
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from backlot.oidc_session import (AuthenticationExpired, AuthenticationUnavailable,
                                 CookieRenewalRequired, OidcConfig, OidcSessions, OperationBusy, UnsupportedLogoutScope)
from backlot.session_vault import VaultUnavailable
from backlot.browser_realm import hz, resolve

COOKIE = "__Host-dofe_montage_sid"
STATE_COOKIE = "__Host-dofe_montage_state"
SUPPRESS_COOKIE = "__Host-dofe_montage_logout"
ASSETS = Path(__file__).parent / "ui"


def local_path(value):
    if not isinstance(value, str):
        return "/"
    decoded = value
    for _ in range(5):
        candidate = unquote(decoded)
        if candidate == decoded:
            break
        decoded = candidate
    if (not decoded.startswith("/") or decoded.startswith("//") or "\\" in decoded
            or "%" in decoded or any(ord(c) < 32 for c in decoded)
            or any(part in {".", ".."} for part in urlsplit(decoded).path.split("/"))):
        return "/"
    return value


def set_cookie(response, name, value, max_age=2592000):
    response.set_cookie(name, value, max_age=max_age, secure=True, httponly=True, samesite="lax", path="/")


def clear_cookie(response, name):
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")


def bridge_page(next_path, prefix=""):
    next_path = prefix + local_path(next_path)
    # No token, ticket, or user-controlled executable content in the document.
    safe = html.escape(local_path(next_path), quote=True)
    return HTMLResponse('<!doctype html><html lang="zh"><meta charset="utf-8"><title>OpenMontage</title>'
                        '<body data-next="' + safe + '">正在恢复应用会话…<script src="' + prefix + '/auth/request-bridge.js" defer></script></body></html>',
                        headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; script-src 'self'; worker-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"})


def install_oidc(app, *, legacy_enabled=False, sessions=None):
    sessions = sessions or OidcSessions(OidcConfig.from_environment())
    config = sessions.config
    app.state.oidc_sessions = sessions

    realm_sessions = {"canonical": sessions}
    if config.public_origin == "https://montage.hozonauto.com" and config.issuer == "https://user.hozonauto.com/api":
        realm_sessions["hz"] = OidcSessions(config, sessions.client, realm=hz(config))

    def selected(request):
        return realm_sessions[request.state.browser_realm.id]

    def names(request, name):
        realm = request.state.browser_realm
        return {COOKIE: realm.cookie, STATE_COOKIE: realm.state_cookie, SUPPRESS_COOKIE: realm.suppress_cookie}.get(name, name)

    def public_path(request, value):
        return request.state.browser_realm.path(value)

    def same_origin(request):
        return request.headers.get("origin") == request.state.browser_realm.origin and request.headers.get("sec-fetch-site", "same-origin") == "same-origin"

    async def payload(request, *, native=False):
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            raise AuthenticationExpired("JSON request required")
        if not same_origin(request) and not (native and "origin" not in request.headers and "sec-fetch-site" not in request.headers):
            raise AuthenticationExpired("Request origin mismatch")
        body = await request.body()
        if len(body) > 32768:
            raise AuthenticationExpired("Request too large")
        try:
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except ValueError as exc:
            raise AuthenticationExpired("Invalid request") from exc

    async def begin_login(request, next_path, interactive=False):
        state, target = await asyncio.to_thread(selected(request).authorization, request.state.browser_realm.base + "/auth/callback", local_path(next_path), interactive)
        response = RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})
        set_cookie(response, names(request, STATE_COOKIE), state, 300)
        return response

    @app.middleware("http")
    async def oidc_gate(request: Request, call_next):
        try:
            realm = resolve(request, config)
        except ValueError:
            return JSONResponse({"error": "INVALID_BROWSER_REALM"}, status_code=400)
        request.state.browser_realm = realm
        if realm.prefix:
            path = request.scope["path"]
            if path != realm.prefix and not path.startswith(realm.prefix + "/"):
                return JSONResponse({"error": "INVALID_BROWSER_MOUNT"}, status_code=400)
            request.scope["path"] = path[len(realm.prefix):] or "/"
            request.scope["raw_path"] = request.scope["path"].encode()
            if hasattr(request, "_url"):
                del request._url
            # The alias never consumes/clears the old same-origin legacy cookie.
            request.state.oidc_base_path = realm.prefix
        path = request.url.path
        if realm.prefix and path in {"/auth/session", "/auth/legacy"}:
            return JSONResponse({"error": "LEGACY_EMBED_DISABLED"}, status_code=403)
        if path.startswith("/auth") or path == "/api/health" or path.startswith("/ui/") or path.startswith("/static/") or path == "/favicon.ico":
            return await call_next(request)
        cookie = request.cookies.get(names(request, COOKIE))
        if not cookie:
            if legacy_enabled and not realm.prefix and request.cookies.get("openmontage_backlot_session"):
                return await call_next(request)  # The existing legacy guard still verifies it.
            if request.method == "GET" and (path == "/" or path.startswith("/p/")):
                if request.cookies.get(names(request, SUPPRESS_COOKIE)):
                    return RedirectResponse(public_path(request, "/auth"), status_code=303)
                return await begin_login(request, str(request.url.path) + ("?" + request.url.query if request.url.query else ""))
            return JSONResponse({"error": "OPENMONTAGE_UNAUTHORIZED"}, status_code=401)
        try:
            site = request.headers.get("sec-fetch-site")
            if site not in {None, "same-origin", "none"} and request.headers.get("sec-fetch-mode") != "navigate":
                raise AuthenticationExpired("Cross-origin business request rejected")
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not same_origin(request):
                raise AuthenticationExpired("Request origin mismatch")
            # An SSE connection/automatic reconnect is observation. Its heartbeats
            # never renew the grant; actual authenticated data polls below do.
            observation = path.endswith("/events")
            body = await request.body()
            digest = hashlib.sha256((request.method + " " + str(request.url.path) + "?" + request.url.query + "\n").encode() + body).hexdigest()
            if observation:
                current = await asyncio.to_thread(selected(request).observe, cookie)
                if not current:
                    raise AuthenticationExpired("Subscription authorization expired")
            else:
                current = await asyncio.to_thread(selected(request).prepare, cookie, digest, request.headers.get("x-dofe-operation-id"))
            request.state.oidc_workspace = current["identity"]["workspace"]
            async def alive():
                try:
                    return await asyncio.to_thread(selected(request).observe, cookie) is not None
                except (AuthenticationUnavailable, VaultUnavailable):
                    return False
            request.state.oidc_session_alive = alive
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            if not observation and response.status_code < 400:
                max_age = max(1, min(2592000, int(current["tokens"]["refresh_expires_at"] / 1000 - time.time())))
                set_cookie(response, names(request, COOKIE), cookie, max_age)
            if "text/html" in response.headers.get("content-type", ""):
                response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
            return response
        except CookieRenewalRequired as exc:
            if "x-dofe-operation-id" not in request.headers and request.headers.get("accept", "").find("text/html") >= 0:
                return bridge_page(str(request.url.path) + ("?" + request.url.query if request.url.query else ""), realm.prefix)
            return JSONResponse({"error": "COOKIE_RENEWAL_REQUIRED", "operation_id": exc.operation_id, "ticket": exc.ticket}, status_code=409, headers={"Cache-Control": "no-store"})
        except OperationBusy:
            return JSONResponse({"error": "OPERATION_ALREADY_DISPATCHED"}, status_code=409)
        except AuthenticationExpired:
            response = JSONResponse({"error": "OPENMONTAGE_UNAUTHORIZED"}, status_code=401)
            try:
                retained = await asyncio.to_thread(selected(request).vault.get, cookie) if cookie else None
            except VaultUnavailable:
                return JSONResponse({"error": "AUTH_UNAVAILABLE"}, status_code=503)
            if retained and retained.get("logout_pending"):
                # Concurrent business polls must not erase the only correlator
                # needed to finish an already suppressed scoped logout.
                set_cookie(response, names(request, COOKIE), cookie)
            else:
                clear_cookie(response, names(request, COOKIE))
            return response
        except (AuthenticationUnavailable, VaultUnavailable):
            return JSONResponse({"error": "AUTH_UNAVAILABLE"}, status_code=503)

    @app.get("/auth")
    async def auth_page(request: Request):
        return HTMLResponse('<!doctype html><html lang="zh"><meta charset="utf-8"><title>OpenMontage</title><body>已退出登录。'
                            '<button id="login">登录 OpenMontage</button><script type="module" src="' + public_path(request, "/auth/login.js") + '"></script></body></html>', headers={"Cache-Control": "no-store"})

    @app.post("/auth/login")
    async def login(request: Request):
        try:
            value = await payload(request)
            previous_cookie = request.cookies.get(names(request, COOKIE))
            previous = selected(request).vault.get(previous_cookie) if previous_cookie else None
            if previous and previous.get("logout_pending"):
                await asyncio.to_thread(selected(request).logout, previous_cookie, previous["logout_pending"]["scope"])
                if previous["tokens"]["session_context"] == "browser":
                    await asyncio.to_thread(selected(request).finish_logout, previous_cookie)
            response = await begin_login(request, value.get("next", "/"), True)
            # fetch cannot follow a cross-origin authorization redirect reliably.
            target = response.headers["location"]
            result = JSONResponse({"url": target}, headers={"Cache-Control": "no-store"})
            result.raw_headers.extend((name, val) for name, val in response.raw_headers if name == b"set-cookie")
            clear_cookie(result, names(request, SUPPRESS_COOKIE))
            return result
        except AuthenticationExpired:
            return JSONResponse({"error": "INVALID_LOGIN_REQUEST"}, status_code=403)
        except (AuthenticationUnavailable, VaultUnavailable):
            return JSONResponse({"error": "LOGOUT_CONFIRMATION_REQUIRED"}, status_code=503)

    @app.get("/auth/callback")
    async def callback(request: Request):
        if request.cookies.get(names(request, SUPPRESS_COOKIE)):
            return JSONResponse({"error": "AUTHORIZATION_SUPPRESSED"}, status_code=401)
        state = request.query_params.get("state", "")
        expected = request.cookies.get(names(request, STATE_COOKIE), "")
        if not state or not expected or not secrets.compare_digest(state, expected):
            return JSONResponse({"error": "INVALID_AUTHORIZATION_STATE"}, status_code=400)
        transaction = await asyncio.to_thread(selected(request).vault.consume, "transaction:" + state)
        if not transaction:
            return JSONResponse({"error": "AUTHORIZATION_EXPIRED"}, status_code=400)
        try:
            if request.query_params.get("error"):
                if not transaction["interactive"] and not request.cookies.get(names(request, SUPPRESS_COOKIE)):
                    return await begin_login(request, transaction["next"], True)
                raise AuthenticationExpired("Authorization was not completed")
            code = request.query_params.get("code", "")
            if not code or len(code) > 4096:
                raise AuthenticationExpired("Invalid authorization code")
            tokens = await asyncio.to_thread(selected(request).client.token, {"grant_type": "authorization_code", "code": code,
                                              "redirect_uri": transaction["redirect_uri"], "code_verifier": transaction["verifier"]})
            cookie, _ = await asyncio.to_thread(selected(request).create, tokens, transaction["nonce"])
            response = RedirectResponse(public_path(request, "/auth/ready?") + urlencode({"next": transaction["next"]}), status_code=303)
            set_cookie(response, names(request, COOKIE), cookie)
            clear_cookie(response, names(request, STATE_COOKIE))
            clear_cookie(response, names(request, SUPPRESS_COOKIE))
            return response
        except AuthenticationExpired:
            return JSONResponse({"error": "AUTHORIZATION_REJECTED"}, status_code=401)
        except (AuthenticationUnavailable, VaultUnavailable):
            return JSONResponse({"error": "AUTH_UNAVAILABLE"}, status_code=503)

    @app.post("/auth/bootstrap")
    async def bootstrap(request: Request):
        try:
            value = await payload(request, native=True)
            ticket = value.get("ticket")
            if not isinstance(ticket, str) or not 16 <= len(ticket) <= 4096:
                raise AuthenticationExpired("Invalid target ticket")
            cookie, _ = await asyncio.to_thread(selected(request).bootstrap, ticket)
            response = JSONResponse({"authenticated": True}, headers={"Cache-Control": "no-store"})
            set_cookie(response, names(request, COOKIE), cookie)
            clear_cookie(response, names(request, SUPPRESS_COOKIE))
            return response
        except AuthenticationExpired:
            return JSONResponse({"error": "BOOTSTRAP_REJECTED"}, status_code=401)
        except (AuthenticationUnavailable, VaultUnavailable):
            return JSONResponse({"error": "AUTH_UNAVAILABLE"}, status_code=503)

    @app.post("/auth/logout")
    async def logout(request: Request):
        try:
            value = await payload(request)
            scope = value.get("scope", "device")
            if scope not in {"client", "device", "all"}:
                raise AuthenticationExpired("Invalid logout scope")
        except AuthenticationExpired:
            return JSONResponse({"error": "INVALID_LOGOUT_REQUEST"}, status_code=403)
        result = {}
        try:
            result = await asyncio.to_thread(selected(request).logout, request.cookies[names(request, COOKIE)], scope) if request.cookies.get(names(request, COOKIE)) else {"loggedOut": True}
            response = JSONResponse(result, headers={"Cache-Control": "no-store"})
            if result.get("loggedOut"):
                clear_cookie(response, names(request, COOKIE))
        except UnsupportedLogoutScope:
            return JSONResponse({"error": "UNSUPPORTED_LOGOUT_SCOPE"}, status_code=400)
        except (AuthenticationExpired, AuthenticationUnavailable, VaultUnavailable):
            # Revocation of a family alone is not proof that this scope ran.
            response = JSONResponse({"loggedOut": False, "serverConfirmed": False}, status_code=503)
        # This opaque correlator remains usable only for logout recovery;
        # pending state cannot authorize business and must survive RP-cookie expiry.
        if not result.get("loggedOut"):
            if request.cookies.get(names(request, COOKIE)):
                set_cookie(response, names(request, COOKIE), request.cookies[names(request, COOKIE)])
        set_cookie(response, names(request, SUPPRESS_COOKIE), "1")
        clear_cookie(response, names(request, STATE_COOKIE))
        if not request.state.browser_realm.prefix:
            clear_cookie(response, "openmontage_backlot_session")
        return response

    @app.post("/auth/logout-confirm")
    async def logout_confirm(request: Request):
        try:
            await payload(request)
            if not request.cookies.get(names(request, COOKIE)):
                raise AuthenticationExpired("Missing logout transaction")
            await asyncio.to_thread(selected(request).finish_logout, request.cookies[names(request, COOKIE)])
            response = JSONResponse({"loggedOut": True}, headers={"Cache-Control": "no-store"})
            clear_cookie(response, names(request, COOKIE))
            set_cookie(response, names(request, SUPPRESS_COOKIE), "1")
            return response
        except AuthenticationExpired:
            return JSONResponse({"error": "INVALID_LOGOUT_CONFIRMATION"}, status_code=403)
        except (AuthenticationUnavailable, VaultUnavailable):
            return JSONResponse({"error": "CENTRAL_LOGOUT_UNCONFIRMED"}, status_code=503)

    @app.post("/auth/backchannel")
    async def backchannel(request: Request):
        if request.state.browser_realm.id != "canonical":
            return JSONResponse({"error": "CANONICAL_BACKCHANNEL_REQUIRED"}, status_code=400)
        try:
            body = await request.body()
            if len(body) > 16384:
                raise ValueError()
            from urllib.parse import parse_qs
            token = parse_qs(body.decode()).get("logout_token", [""])[0]
            header = jwt.get_unverified_header(token)
            jwks = await asyncio.to_thread(lambda: selected(request).client.request("GET", selected(request).client.discovery()["jwks_uri"]))
            keys = [key for key in jwks.get("keys", []) if key.get("kid") == header.get("kid") and key.get("kty") == "RSA"]
            if header.get("alg") != "RS256" or len(keys) != 1:
                raise ValueError()
            claims = jwt.decode(token, jwt.PyJWK.from_dict(keys[0]).key, algorithms=["RS256"], audience=config.client_id,
                                issuer=config.issuer, options={"require": ["iss", "aud", "iat", "jti", "sid"]})
            if "nonce" in claims or abs(time.time() - claims["iat"]) > 300 or "http://schemas.openid.net/event/backchannel-logout" not in claims.get("events", {}):
                raise ValueError()
            key = "logout-jti:" + claims["jti"]
            with selected(request).vault.lock(key):
                if selected(request).vault.get(key):
                    raise ValueError()
                selected(request).vault.revoke_sid(claims["sid"])
                selected(request).vault.put(key, {}, time.time() + 600, kind="replay")
            return JSONResponse({"ok": True})
        except (ValueError, jwt.PyJWTError, KeyError, TypeError):
            return JSONResponse({"error": "INVALID_LOGOUT_TOKEN"}, status_code=400)
        except (AuthenticationUnavailable, VaultUnavailable):
            return JSONResponse({"error": "AUTH_UNAVAILABLE"}, status_code=503)

    @app.get("/auth/status")
    async def status(request: Request):
        # A local UI hint only. Every business handler still requires prepare;
        # polling this endpoint never rotates or confirms authority.
        cookie = request.cookies.get(names(request, COOKIE))
        current = await asyncio.to_thread(selected(request).vault.get, cookie) if cookie else None
        return JSONResponse({"sessionHint": bool(current), "logoutPending": bool(current and current.get("logout_pending")),
                             "logoutScope": current.get("logout_pending", {}).get("scope") if current else None},
                            headers={"Cache-Control": "no-store"})

    @app.get("/auth/ready")
    async def ready(request: Request):
        return bridge_page(request.query_params.get("next", "/"), request.state.browser_realm.prefix)

    @app.get("/auth/session-worker.js")
    async def worker(request: Request):
        source = (ASSETS / "session-worker.js").read_text()
        return HTMLResponse("const IDENTITY_ORIGIN=" + json.dumps(request.state.browser_realm.identity) + ";\nconst APP_BASE=" + json.dumps(request.state.browser_realm.prefix) + ";\n" + source, media_type="application/javascript",
                            headers={"Cache-Control": "no-store", "Service-Worker-Allowed": request.state.browser_realm.prefix + "/"})

    @app.get("/auth/request-bridge.js")
    async def bridge_script(request: Request):
        return HTMLResponse("const APP_BASE=" + json.dumps(request.state.browser_realm.prefix) + ";\n" + (ASSETS / "request-bridge.js").read_text(), media_type="application/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/auth/logout-client.js")
    async def logout_client(request: Request):
        return HTMLResponse("const IDENTITY_ORIGIN=" + json.dumps(request.state.browser_realm.identity) + ";\nconst APP_BASE=" + json.dumps(request.state.browser_realm.prefix) + ";\n" + (ASSETS / "session-logout.js").read_text(),
                            media_type="application/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/auth/login.js")
    async def login_script(request: Request):
        return HTMLResponse("import {restoreLogout} from " + json.dumps(public_path(request, "/auth/logout-client.js")) + "; await restoreLogout(); document.querySelector('#login').onclick=async()=>{const r=await fetch(" + json.dumps(public_path(request, "/auth/login")) + ",{method:'POST',headers:{'content-type':'application/json'},body:'{}'});const v=await r.json();if(r.ok)location.assign(v.url);};", media_type="application/javascript", headers={"Cache-Control": "no-store"})

    return sessions
