"""Real HTTPS/ServiceWorker/Cookie contract; fake OP authority, no production access."""
from __future__ import annotations

import datetime
import ipaddress
import os
import socket
import threading
import time
from dataclasses import replace

import requests
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from playwright.sync_api import sync_playwright

from backlot.oidc_routes import COOKIE, install_oidc
from backlot.oidc_session import OidcSessions
from test_oidc_session import manager  # noqa: F401 - shared isolated vault fixture


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def certificate(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, '127.0.0.1')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(key, hashes.SHA256()))
    private_path, cert_path = tmp_path / 'test.key', tmp_path / 'test.pem'
    private_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    private_path.chmod(0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(private_path), str(cert_path)


def test_worker_rotation_and_browser_logout_survive_reload_until_actual_op_cookie_confirmation(manager, tmp_path):
    original, identity, cookie = manager
    rp_port, op_port = port(), port()
    rp_origin, op_origin = f'https://127.0.0.1:{rp_port}', f'https://127.0.0.1:{op_port}'
    config = replace(original.config, issuer=op_origin + '/api', public_origin=rp_origin)
    sessions = OidcSessions(config, identity)
    value = sessions.vault.get(cookie)
    value['tokens']['session_context'] = identity.context = 'browser'
    sessions.save(cookie, value)
    rp, op = FastAPI(), FastAPI()
    op.add_middleware(CORSMiddleware, allow_origins=[rp_origin], allow_credentials=True,
                      allow_methods=['POST'], allow_headers=['content-type'])
    state = {'business': 0, 'failed_completion': False, 'cookie_confirmed': False}

    @rp.get('/')
    async def index():
        return HTMLResponse('<!doctype html><button id="logout">退出登录</button><script type="module">'
                            'import {logoutFromUI} from "/auth/logout-client.js";'
                            'document.querySelector("#logout").onclick=()=>logoutFromUI(document.querySelector("#logout"));</script>')

    @rp.get('/api/data')
    async def data():
        state['business'] += 1
        return {'accepted': state['business']}

    install_oidc(rp, sessions=sessions)

    @op.post('/api/auth/session/renew-cookie')
    async def renew(request: Request):
        assert request.cookies.get('__Host-dofe_sso')
        ticket = (await request.json())['ticket']
        operation = ticket.removeprefix('opaque-ticket-for-')
        result = JSONResponse({'challenge': 'challenge:' + operation})
        result.set_cookie('__Host-dofe_sso', 'renewed:' + operation, secure=True, httponly=True, samesite='lax', path='/')
        return result

    @op.post('/api/auth/session/v2/confirm-cookie')
    async def confirm(request: Request):
        operation = (await request.json())['challenge'].removeprefix('challenge:')
        assert request.cookies.get('__Host-dofe_sso') == 'renewed:' + operation
        identity.confirmed.add(operation)
        return {'confirmed': True}

    @op.post('/api/auth/session/v2/logout-complete')
    async def logout_complete(request: Request):
        assert (await request.json())['logout_ticket']
        if not state['failed_completion']:
            state['failed_completion'] = True
            return JSONResponse({'error': 'temporary_network_failure'}, status_code=503)
        assert request.cookies.get('__Host-dofe_sso')
        response = JSONResponse({'logged_out': True})
        response.delete_cookie('__Host-dofe_sso', secure=True, httponly=True, samesite='lax', path='/')
        response.set_cookie('__Host-dofe_logout', 'actual-suppression', secure=True, httponly=True, samesite='lax', path='/')
        return response

    @op.post('/api/auth/session/v2/logout-cookie-confirm')
    async def logout_cookie_confirm(request: Request):
        assert (await request.json())['logout_ticket']
        assert '__Host-dofe_sso' not in request.cookies
        assert request.cookies.get('__Host-dofe_logout') == 'actual-suppression'
        identity.logout_confirmed = state['cookie_confirmed'] = True
        return {'confirmed': True}

    key, cert = certificate(tmp_path)
    servers = [uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=bind, ssl_keyfile=key, ssl_certfile=cert,
                                           log_level='error', lifespan='off')) for app, bind in [(rp, rp_port), (op, op_port)]]
    threads = [threading.Thread(target=server.run, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        for _ in range(100):
            if all(server.started for server in servers):
                break
            time.sleep(.05)
        assert all(server.started for server in servers)
        with sync_playwright() as pw:
            options = {'headless': True, 'args': ['--ignore-certificate-errors']}
            if os.environ.get('OPENMONTAGE_TEST_CHROME_PATH'):
                options['executable_path'] = os.environ['OPENMONTAGE_TEST_CHROME_PATH']
            browser = pw.chromium.launch(**options)
            try:
                context = browser.new_context(ignore_https_errors=True)
                context.add_cookies([{'name': COOKIE, 'value': cookie, 'url': rp_origin, 'secure': True, 'httpOnly': True, 'sameSite': 'Lax'},
                                     {'name': '__Host-dofe_sso', 'value': 'central-identity', 'url': op_origin, 'secure': True, 'httpOnly': True, 'sameSite': 'Lax'}])
                page = context.new_page()
                page.goto(rp_origin + '/auth/ready?next=/', wait_until='domcontentloaded')
                page.wait_for_url(rp_origin + '/')
                page.wait_for_function('navigator.serviceWorker.controller !== null')
                for expected in (1, 2):
                    assert page.evaluate('async()=>await (await fetch("/api/data")).json()') == {'accepted': expected}
                assert identity.generation == 3  # root navigation plus two actual business reads
                page.get_by_role('button', name='退出登录').click()
                page.get_by_role('button', name='本机已退出，点击重试确认服务器退出').wait_for()
                assert sessions.vault.get(cookie)['logout_pending']['ticket']
                assert not state['cookie_confirmed']
                sessions.vault.revoke_sid('sid')  # outbox delivery cannot erase pending completion
                page.goto(rp_origin + '/auth', wait_until='domcontentloaded')
                page.get_by_role('button', name='继续完成退出登录').click()
                page.get_by_role('button', name='登录 OpenMontage').wait_for(state='visible')
                assert state['cookie_confirmed']
                assert sessions.vault.get(cookie) is None
                names = {item['name'] for item in context.cookies()}
                assert '__Host-dofe_sso' not in names and COOKIE not in names
                assert '__Host-dofe_logout' in names
                assert identity.generation == 3  # logout/reload/status never refresh revoked RT
            finally:
                browser.close()
    finally:
        for server in servers:
            server.should_exit = True
        for thread in threads:
            thread.join(timeout=5)


def test_alias_real_worker_keeps_legacy_origin_cookie_storage_and_root_worker(manager, tmp_path, monkeypatch):
    """Real TLS/browser path mount; the test edge supplies the production fixed tuple."""
    import backlot.browser_realm as realm_module
    import backlot.oidc_routes as routes
    from backlot.browser_realm import HZ_PREFIX
    from test_oidc_session import tokens
    original, identity, old_cookie = manager
    original.vault.delete(old_cookie)
    bind = port()
    origin = f'https://127.0.0.1:{bind}'
    config = replace(original.config, issuer='https://user.hozonauto.com/api', public_origin='https://montage.hozonauto.com')
    actual_hz = realm_module.hz(config)
    # Only test transport differs; production resolver tuple is exercised unchanged.
    test_hz = replace(actual_hz, base=origin + HZ_PREFIX, identity=origin + '/hz/user/api')
    monkeypatch.setattr(realm_module, 'hz', lambda _: test_hz)
    monkeypatch.setattr(routes, 'hz', lambda _: test_hz)
    primary = OidcSessions(config, identity)
    alias = OidcSessions(config, identity, realm=test_hz)
    identity.context = 'browser'
    cookie, _ = alias.create(tokens(context='browser'))
    rp, op = FastAPI(), FastAPI()
    state = {'business': 0, 'renew': 0}

    @rp.get('/')
    async def index():
        state['business'] += 1
        return HTMLResponse('<button id="logout">退出登录</button><script type="module">'
                            f'import {{logoutFromUI}} from "{HZ_PREFIX}/auth/logout-client.js";'
                            'document.querySelector("#logout").onclick=()=>logoutFromUI(document.querySelector("#logout"));</script>')

    @rp.get('/api/data')
    async def data():
        state['business'] += 1
        return {'accepted': state['business']}

    install_oidc(rp, sessions=primary)

    @op.get('/')
    async def legacy_page():
        return HTMLResponse('<!doctype html>legacy')

    @op.get('/legacy-worker.js')
    async def legacy_worker():
        return HTMLResponse("self.addEventListener('install',e=>e.waitUntil(self.skipWaiting()));self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));", media_type='application/javascript')

    @op.post('/hz/user/api/auth/session/renew-cookie')
    async def renew(request: Request):
        state['renew'] += 1
        assert request.cookies.get('__Host-hz-dofe_sso')
        operation = (await request.json())['ticket'].removeprefix('opaque-ticket-for-')
        result = JSONResponse({'challenge': operation})
        result.set_cookie('__Host-hz-dofe_sso', operation, secure=True, httponly=True, path='/')
        return result

    @op.post('/hz/user/api/auth/session/v2/confirm-cookie')
    async def confirm(request: Request):
        operation = (await request.json())['challenge']
        assert request.cookies['__Host-hz-dofe_sso'] == operation
        identity.confirmed.add(operation)
        return {'confirmed': True}

    @op.post('/hz/user/api/auth/session/v2/logout-complete')
    async def logout_complete(request: Request):
        assert (await request.json())['logout_ticket']
        result = JSONResponse({'logged_out': True})
        result.delete_cookie('__Host-hz-dofe_sso', path='/', secure=True)
        result.set_cookie('__Host-hz-dofe_logout', 'suppressed', secure=True, httponly=True, path='/')
        return result

    @op.post('/hz/user/api/auth/session/v2/logout-cookie-confirm')
    async def logout_confirm(request: Request):
        assert '__Host-hz-dofe_sso' not in request.cookies
        assert request.cookies['__Host-hz-dofe_logout'] == 'suppressed'
        identity.logout_confirmed = True
        return {'confirmed': True}

    async def edge(scope, receive, send):
        if scope['type'] == 'http' and scope['path'].startswith(HZ_PREFIX + '/'):
            # The real reverse proxy overwrites all five fields, never caller input.
            metadata = {'host': 'montage.hozonauto.com', 'x-external-base': 'https://ixicai.cn/hz/montage',
                        'x-forwarded-host': 'ixicai.cn', 'x-forwarded-prefix': HZ_PREFIX, 'x-forwarded-proto': 'https'}
            scope = dict(scope)
            scope['headers'] = [(key, value) for key, value in scope['headers'] if key.decode().lower() not in metadata]
            scope['headers'] += [(key.encode(), value.encode()) for key, value in metadata.items()]
            return await rp(scope, receive, send)
        return await op(scope, receive, send)

    key, cert = certificate(tmp_path)
    server = uvicorn.Server(uvicorn.Config(edge, host='127.0.0.1', port=bind, ssl_keyfile=key, ssl_certfile=cert,
                                          log_level='error', lifespan='off', proxy_headers=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        with sync_playwright() as pw:
            options = {'headless': True, 'args': ['--ignore-certificate-errors']}
            if os.environ.get('OPENMONTAGE_TEST_CHROME_PATH'):
                options['executable_path'] = os.environ['OPENMONTAGE_TEST_CHROME_PATH']
            browser = pw.chromium.launch(**options)
            try:
                context = browser.new_context(ignore_https_errors=True)
                context.add_cookies([{'name': name, 'value': value, 'url': origin, 'secure': True, 'httpOnly': True}
                                     for name, value in [(test_hz.cookie, cookie), ('__Host-hz-dofe_sso', 'central'),
                                                         ('legacy_identity', 'unchanged'), (COOKIE, 'old-site-opaque')]])
                legacy = context.new_page()
                legacy.goto(origin + '/')
                legacy.evaluate("async()=>{localStorage.setItem('backlot.theme','old-site');localStorage.setItem('token','legacy-value');await navigator.serviceWorker.register('/legacy-worker.js',{scope:'/'});await navigator.serviceWorker.ready;}")
                page = context.new_page()
                page.goto(origin + HZ_PREFIX + '/auth/ready?next=/', wait_until='domcontentloaded')
                page.wait_for_url(origin + HZ_PREFIX + '/')
                page.wait_for_function('navigator.serviceWorker.controller !== null')
                assert page.evaluate("async()=>await (await fetch('/hz/montage/api/data')).json()")['accepted'] == 2
                assert state['renew'] == 2
                scopes = page.evaluate('async()=>(await navigator.serviceWorker.getRegistrations()).map(r=>r.scope)')
                assert set(scopes) == {origin + '/', origin + HZ_PREFIX + '/'}
                page.get_by_role('button', name='退出登录').click()
                page.wait_for_url(origin + HZ_PREFIX + '/auth')
                assert alias.vault.get(cookie) is None
                names = {item['name']: item['value'] for item in context.cookies()}
                assert names['legacy_identity'] == 'unchanged' and names[COOKIE] == 'old-site-opaque'
                assert test_hz.cookie not in names and '__Host-hz-dofe_sso' not in names
                assert legacy.evaluate("localStorage.getItem('backlot.theme')") == 'old-site'
                assert legacy.evaluate("localStorage.getItem('token')") == 'legacy-value'
                assert legacy.evaluate('navigator.serviceWorker.controller.scriptURL') == origin + '/legacy-worker.js'
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
