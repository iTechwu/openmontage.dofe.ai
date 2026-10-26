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
