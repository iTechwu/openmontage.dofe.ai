"""OIDC confidential RP and durable request authentication, separate from MCP billing."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlparse

import jwt
import requests

from backlot.session_vault import SessionVault


class AuthenticationExpired(RuntimeError):
    pass


class AuthenticationUnavailable(RuntimeError):
    pass


class CookieRenewalRequired(RuntimeError):
    def __init__(self, operation_id: str, ticket: str):
        self.operation_id = operation_id
        self.ticket = ticket


class OperationBusy(RuntimeError):
    pass


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str
    public_origin: str
    session_db: Path
    session_key: str
    tenant_context_url: str
    internal_secret: str

    @classmethod
    def from_environment(cls):
        def required(name):
            value = os.environ.get("OPENMONTAGE_OIDC_" + name, "").strip()
            if not value:
                raise RuntimeError("Missing OpenMontage OIDC configuration: " + name)
            return value
        config = cls(required("ISSUER").rstrip("/"), required("CLIENT_ID"), required("CLIENT_SECRET"),
                     required("PUBLIC_ORIGIN").rstrip("/"), Path(required("SESSION_DB")), required("SESSION_KEY"),
                     required("TENANT_CONTEXT_URL"), required("INTERNAL_SECRET"))
        for value in (config.issuer, config.public_origin, config.tenant_context_url):
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise RuntimeError("OIDC URLs must be exact trusted HTTPS URLs")
        if urlparse(config.public_origin).path:
            raise RuntimeError("OIDC public origin must not include a path")
        return config


def token_response(value: dict) -> dict:
    strings = ("access_token", "refresh_token", "session_id", "family_id")
    numbers = ("refresh_generation", "idle_expires_at", "refresh_expires_at", "auth_epoch")
    if (not isinstance(value, dict) or value.get("session_version") != "2" or (not isinstance(value.get("expires_in"), int) or isinstance(value.get("expires_in"), bool) or not 0 <= value["expires_in"] <= 300)
            or any(not isinstance(value.get(name), str) or not value[name] for name in strings)
            or any(not isinstance(value.get(name), int) or isinstance(value[name], bool) for name in numbers)
            or value.get("session_context") not in {"native", "browser"}):
        raise AuthenticationUnavailable("Invalid session-v2 response")
    expires_at = int(time.time() * 1000) + value["expires_in"] * 1000
    try:
        # This only shortens a scheduling hint from the trusted token response;
        # it never authenticates a claim (live introspection does that).
        claim = jwt.decode(value["access_token"], options={"verify_signature": False}).get("exp")
        if isinstance(claim, (int, float)) and not isinstance(claim, bool):
            expires_at = min(expires_at, int(claim * 1000))
    except jwt.PyJWTError:
        pass
    return {**value, "access_expires_at": expires_at}


class OidcClient:
    def __init__(self, config: OidcConfig):
        self.config = config
        self._discovery = None
        self._discovery_until = 0

    def discovery(self):
        if self._discovery and self._discovery_until > time.time():
            return self._discovery
        value = self.request("GET", self.config.issuer + "/.well-known/openid-configuration")
        if value.get("issuer") != self.config.issuer:
            raise AuthenticationUnavailable("OIDC issuer mismatch")
        issuer = urlparse(self.config.issuer)
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            endpoint = urlparse(value.get(name, ""))
            if endpoint.scheme != "https" or endpoint.netloc != issuer.netloc or not endpoint.path.startswith(issuer.path + "/") or endpoint.username or endpoint.password:
                raise AuthenticationUnavailable("Untrusted OIDC endpoint")
        self._discovery, self._discovery_until = value, time.time() + 60
        return value

    @staticmethod
    def request(method, url, **kwargs):
        try:
            response = requests.request(method, url, timeout=15, allow_redirects=False, **kwargs)
            value = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise AuthenticationUnavailable("Identity service unavailable") from exc
        if not isinstance(value, dict):
            raise AuthenticationUnavailable("Invalid identity response")
        if isinstance(value.get("data"), dict):
            value = value["data"]
        if not response.ok:
            if value.get("error") in {"invalid_grant", "invalid_token"}:
                raise AuthenticationExpired("Authorization was revoked or expired")
            raise AuthenticationUnavailable("Identity request was rejected")
        if not isinstance(value, dict):
            raise AuthenticationUnavailable("Invalid identity response")
        return value

    def token(self, body):
        return token_response(self.request("POST", self.discovery()["token_endpoint"], data={
            **body, "client_id": self.config.client_id, "client_secret": self.config.client_secret, "session_version": "2"}))

    def operation(self, path, body):
        return self.request("POST", self.config.issuer + path, json={
            **body, "client_id": self.config.client_id, "client_secret": self.config.client_secret})

    def identity(self, tokens):
        value = self.operation("/auth/session/v2/introspect", {"token": tokens["access_token"]})
        if (value.get("active") is not True or value.get("client_id") != self.config.client_id
                or value.get("session_id") != tokens["session_id"] or value.get("family_id") != tokens["family_id"]
                or value.get("auth_epoch") != tokens["auth_epoch"] or value.get("session_context") != tokens["session_context"]
                or not value.get("sub") or not value.get("tenant_id") or not value.get("sso_team_id")
                or not isinstance(value.get("exp"), int) or value["exp"] <= time.time()):
            raise AuthenticationExpired("Application session is not active")
        timestamp = str(int(time.time()))
        signature = hmac.new(self.config.internal_secret.encode(), (timestamp + ":openmontage-rp").encode(), hashlib.sha256).hexdigest()
        context = self.request("POST", self.config.tenant_context_url,
                               headers={"authorization": "Bearer " + timestamp + ":" + signature + ":openmontage-rp",
                                        "x-service-name": "openmontage-rp"},
                               json={"accessToken": tokens["access_token"]})
        context = context.get("data", context)
        if value["exp"] <= time.time():
            raise AuthenticationExpired("Access authorization expired during workspace verification")
        tokens["access_expires_at"] = value["exp"] * 1000
        if (context.get("ssoTeamId") != value["sso_team_id"] or context.get("ssoSub") != value["sub"]
                or not isinstance(context.get("tenantId"), str) or not context["tenantId"]):
            raise AuthenticationExpired("Workspace identity mapping unavailable")
        return {"sub": value["sub"], "sso_tenant_id": value["tenant_id"], "sso_team_id": value["sso_team_id"],
                "workspace": "tenant:" + context["tenantId"]}

    def verify_id_token(self, tokens, nonce=None):
        try:
            compact = tokens["id_token"]
            header = jwt.get_unverified_header(compact)
            keys = self.request("GET", self.discovery()["jwks_uri"]).get("keys", [])
            matching = [key for key in keys if key.get("kid") == header.get("kid") and key.get("kty") == "RSA"]
            if header.get("alg") != "RS256" or len(matching) != 1:
                raise ValueError("Invalid token key")
            claims = jwt.decode(compact, jwt.PyJWK.from_dict(matching[0]).key, algorithms=["RS256"],
                                issuer=self.config.issuer, audience=self.config.client_id,
                                options={"require": ["iss", "aud", "sub", "exp", "iat"]})
            if claims.get("sid") != tokens["session_id"] or nonce is not None and claims.get("nonce") != nonce:
                raise ValueError("Invalid authorization binding")
            return claims
        except (jwt.PyJWTError, KeyError, ValueError) as exc:
            raise AuthenticationExpired("Invalid identity token") from exc


class OidcSessions:
    def __init__(self, config: OidcConfig, client=None):
        self.config, self.client = config, client or OidcClient(config)
        self.vault = SessionVault(config.session_db, config.session_key)

    def save(self, cookie, session):
        token = session["tokens"]
        pending = bool(session.get("logout_pending"))
        expires = max(token["refresh_expires_at"] / 1000, time.time() + 31 * 86400) if pending else token["refresh_expires_at"] / 1000
        self.vault.put(cookie, session, expires, kind="logout" if pending else "session",
                       sid=token["session_id"], family=token["family_id"])

    def create(self, tokens, nonce=None, cookie=None):
        claims = self.client.verify_id_token(tokens, nonce)
        identity = self.client.identity(tokens)
        if claims["sub"] != identity["sub"]:
            raise AuthenticationExpired("Subject mismatch")
        cookie = cookie or secrets.token_urlsafe(32)
        session = {"tokens": tokens, "identity": identity, "csrf": secrets.token_urlsafe(32), "completed": []}
        self.save(cookie, session)
        return cookie, session

    def bootstrap(self, ticket):
        key = "bootstrap:" + hashlib.sha256(ticket.encode()).hexdigest()
        with self.vault.lock(key):
            pending = self.vault.get(key)
            if pending and pending.get("cookie"):
                cookie = pending["cookie"]
                session = self.vault.get(cookie)
                if session:
                    return cookie, session
                if not pending.get("tokens"):
                    raise AuthenticationExpired("Bootstrap session is no longer active")
            if not pending:
                tokens = token_response(self.client.operation("/auth/session/v2/bootstrap-exchange", {"ticket": ticket}))
                pending = {"tokens": tokens, "cookie": secrets.token_urlsafe(32)}
                self.vault.put(key, pending, time.time() + 120, kind="bootstrap")
            cookie, session = self.create(pending["tokens"], cookie=pending["cookie"])
            self.vault.put(key, {"cookie": cookie}, time.time() + 120, kind="bootstrap")
            return cookie, session

    def prepare(self, cookie, digest, operation_id=None):
        operation_id = operation_id or str(uuid.uuid4())
        try:
            uuid.UUID(operation_id)
        except ValueError as exc:
            raise AuthenticationExpired("Invalid operation") from exc
        with self.vault.lock(cookie):
            session = self.vault.get(cookie)
            if not session or session.get("logout_pending"):
                raise AuthenticationExpired("No application session")
            if operation_id in session.get("completed", []):
                raise OperationBusy("A dispatched business operation cannot be replayed")
            # Drain an abandoned authentication intent before accepting this
            # request. Only authentication is recovered; its old business action
            # is never dispatched. Browser cookie confirmation stays mandatory.
            pending = session.get("request_pending")
            if pending and (pending["operation_id"] != operation_id or pending["request_digest"] != digest):
                self._authenticate(cookie, session, pending)
                session["completed"] = (session.get("completed", []) + [pending["operation_id"]])[-64:]
                del session["request_pending"]
                self.save(cookie, session)
                pending = None
            if not pending:
                pending = {"operation_id": operation_id, "request_digest": digest, "phase": "rotation_pending"}
                session["request_pending"] = pending
                self.save(cookie, session)
            self._authenticate(cookie, session, pending)
            session["completed"] = (session.get("completed", []) + [operation_id])[-64:]
            del session["request_pending"]
            self.save(cookie, session)
            return session

    def _authenticate(self, cookie, session, pending, renewals=0):
        token = session["tokens"]
        if pending["phase"] == "authenticated" and token["access_expires_at"] <= time.time() * 1000 + 5000:
            # Cookie confirmation may take longer than the AT lifetime. Renew
            # authentication with a new durable sub-operation, retaining the
            # browser's logical operation so its business action still runs once.
            pending["refresh_operation_id"] = str(uuid.uuid4())
            pending["phase"] = "rotation_pending"
            self.save(cookie, session)
        operation_id = pending.get("refresh_operation_id", pending["operation_id"])
        digest = pending["request_digest"]
        if pending["phase"] == "rotation_pending":
            replacement = self.client.token({"grant_type": "refresh_token", "refresh_token": token["refresh_token"],
                                            "operation_id": operation_id, "request_digest": digest})
            if any(replacement[field] != token[field] for field in ("session_id", "family_id", "auth_epoch", "session_context")) or replacement["refresh_generation"] != token["refresh_generation"] + 1:
                raise AuthenticationExpired("Session generation mismatch")
            session["tokens"] = token = replacement
            pending["phase"] = "authenticated"
            self.save(cookie, session)
        if token["access_expires_at"] <= time.time() * 1000 + 5000:
            # A long-offline recovery returns the original AT with remaining
            # expires_in=0. Its RT is already durable above. A distinct auth
            # operation now obtains a live AT for this still-pending business.
            if renewals >= 2:
                raise AuthenticationUnavailable("A live access token is unavailable")
            return self._authenticate(cookie, session, pending, renewals + 1)
        if token["session_context"] == "browser":
            body = {name: token[name] for name in ("session_id", "family_id")}
            body.update(operation_id=operation_id, request_digest=digest)
            confirmation = self.client.operation("/auth/session/v2/confirmation", body)
            if confirmation.get("active") is not True:
                raise AuthenticationExpired("Session was revoked")
            if confirmation.get("confirmed") is not True:
                ticket = self.client.operation("/auth/session/v2/renew-ticket", body).get("ticket")
                if not isinstance(ticket, str) or not ticket:
                    raise AuthenticationUnavailable("Cookie renewal unavailable")
                raise CookieRenewalRequired(operation_id, ticket)
        if self.client.identity(token) != session["identity"]:
            raise AuthenticationExpired("Application identity changed")

    def observe(self, cookie):
        session = self.vault.get(cookie) if cookie else None
        if not session or session.get("logout_pending"):
            return None
        try:
            self.client.identity(session["tokens"])
        except AuthenticationExpired:
            return None
        return session

    def logout(self, cookie, scope):
        if scope not in {"client", "device", "all"}:
            raise AuthenticationExpired("Invalid logout scope")
        with self.vault.lock(cookie):
            session = self.vault.get(cookie, allow_expired=True)
            if not session:
                return {"loggedOut": True, "session_context": "native"}
            token = session["tokens"]
            pending = session.setdefault("logout_pending", {"scope": scope, "operation_id": str(uuid.uuid4()),
                "request_digest": hashlib.sha256(("logout:" + scope).encode()).hexdigest(),
                "access_token": token["access_token"], "access_expires_at": token["access_expires_at"]})
            if pending["scope"] != scope:
                raise AuthenticationUnavailable("Complete the pending logout scope first")
            self.save(cookie, session)
            if not pending.get("ticket"):
                # Always recover with the original AT/op first, even after AT
                # expiry. A revoked RT is not evidence that this logout scope ran.
                for attempt in range(2):
                    try:
                        result = self.client.operation("/auth/session/v2/logout", {
                            "scope": pending["scope"], "operation_id": pending["operation_id"],
                            "request_digest": pending["request_digest"], "access_token": pending["access_token"]})
                        if result.get("revoked") is not True or not isinstance(result.get("logout_ticket"), str):
                            raise AuthenticationUnavailable("Invalid logout acknowledgement")
                        pending["ticket"] = result["logout_ticket"]
                        self.save(cookie, session)
                        break
                    except AuthenticationExpired as exc:
                        if attempt or pending["access_expires_at"] > time.time() * 1000:
                            raise AuthenticationUnavailable("Logout outcome is unconfirmed") from exc
                        previous = session.get("request_pending")
                        if previous and previous["phase"] == "rotation_pending":
                            try:
                                replacement = self.client.token({"grant_type": "refresh_token", "refresh_token": token["refresh_token"],
                                    "operation_id": previous.get("refresh_operation_id", previous["operation_id"]), "request_digest": previous["request_digest"]})
                            except AuthenticationExpired as rejected:
                                raise AuthenticationUnavailable("Logout outcome is unconfirmed") from rejected
                            session["tokens"] = token = replacement
                            del session["request_pending"]
                            self.save(cookie, session)
                        rotation = pending.setdefault("rotation", {"operation_id": str(uuid.uuid4()),
                            "request_digest": hashlib.sha256(("logout-authorization:" + pending["operation_id"]).encode()).hexdigest()})
                        self.save(cookie, session)
                        try:
                            replacement = self.client.token({"grant_type": "refresh_token", "refresh_token": token["refresh_token"], **rotation})
                        except AuthenticationExpired as rejected:
                            raise AuthenticationUnavailable("Logout outcome is unconfirmed") from rejected
                        if any(replacement[field] != token[field] for field in ("session_id", "family_id", "auth_epoch", "session_context")) or replacement["refresh_generation"] != token["refresh_generation"] + 1:
                            raise AuthenticationUnavailable("Logout authorization identity mismatch")
                        session["tokens"] = token = replacement
                        pending.update(access_token=token["access_token"], access_expires_at=token["access_expires_at"])
                        self.save(cookie, session)
            result = {"logout_ticket": pending["ticket"], "session_context": token["session_context"]}
            if token["session_context"] == "native":
                self.vault.delete(cookie)
                result["loggedOut"] = True
            return result

    def finish_logout(self, cookie):
        with self.vault.lock(cookie):
            session = self.vault.get(cookie, allow_expired=True)
            if not session:
                raise AuthenticationExpired("Logout transaction unavailable")
            pending = session.get("logout_pending", {})
            if not pending.get("ticket"):
                raise AuthenticationUnavailable("Logout is not acknowledged")
            if session["tokens"]["session_context"] == "browser":
                result = self.client.operation("/auth/session/v2/logout-confirmation", {"logout_ticket": pending["ticket"]})
                if result.get("confirmed") is not True:
                    raise AuthenticationUnavailable("Central cookie logout is not confirmed")
            self.vault.delete(cookie)

    def authorization(self, redirect_uri, next_path, interactive=False):
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.vault.put("transaction:" + state, {"state": state, "nonce": nonce, "verifier": verifier,
                       "redirect_uri": redirect_uri, "next": next_path, "interactive": interactive}, time.time() + 300, kind="transaction")
        query = {"client_id": self.config.client_id, "session_version": "2", "response_type": "code", "scope": "openid profile email tenant offline_access",
                 "redirect_uri": redirect_uri, "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
        if not interactive:
            query["prompt"] = "none"
        return state, self.client.discovery()["authorization_endpoint"] + "?" + urlencode(query)
