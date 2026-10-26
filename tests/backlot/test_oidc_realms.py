"""Fixed edge contexts, cryptographic vault separation, no old-origin side effects."""
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backlot.browser_realm import HZ_BASE, HZ_PREFIX, hz
from backlot.oidc_routes import install_oidc, local_path
from backlot.oidc_session import OidcSessions, AuthenticationUnavailable
from backlot.session_vault import VaultUnavailable
from test_oidc_session import manager, tokens, digest  # noqa: F401

HEADERS = {"host": "montage.hozonauto.com", "x-external-base": HZ_BASE,
           "x-forwarded-host": "ixicai.cn", "x-forwarded-prefix": HZ_PREFIX,
           "x-forwarded-proto": "https"}

@pytest.fixture
def realms(manager):
    original, identity, existing_cookie = manager
    original.vault.delete(existing_cookie)
    config = replace(original.config, issuer="https://user.hozonauto.com/api", public_origin="https://montage.hozonauto.com")
    primary = OidcSessions(config, identity)
    alias = OidcSessions(config, identity, realm=hz(config))
    app = FastAPI()
    @app.get('/api/data')
    async def business():
        return {"accepted": True}
    install_oidc(app, sessions=primary, legacy_enabled=True)
    return primary, alias, identity, TestClient(app, base_url=config.public_origin)


def test_alias_fixed_authorize_redirect_and_state_transaction_cannot_cross_realm(realms):
    primary, alias, _, client = realms
    response = client.get(HZ_PREFIX + '/', headers=HEADERS, follow_redirects=False)
    assert response.status_code == 303
    target = urlsplit(response.headers['location'])
    assert target.scheme + '://' + target.netloc + target.path == 'https://ixicai.cn/hz/user/api/oauth/authorize'
    query = parse_qs(target.query)
    assert query['redirect_uri'] == [HZ_BASE + '/auth/callback']
    assert query['session_version'] == ['2']
    state = query['state'][0]
    assert primary.vault.get('transaction:' + state) is None
    assert alias.vault.get('transaction:' + state)['redirect_uri'] == HZ_BASE + '/auth/callback'
    assert '__Host-hz-dofe_rp_montage_state=' in response.headers['set-cookie']
    assert 'Domain=' not in response.headers['set-cookie'] and 'Path=/' in response.headers['set-cookie']


@pytest.mark.parametrize('header', list(HEADERS))
def test_unknown_or_partial_proxy_tuple_rejected_before_any_route(realms, header):
    *_, client = realms
    invalid = {**HEADERS, header: 'invalid'}
    assert client.get(HZ_PREFIX + '/auth/status', headers=invalid).status_code == 400
    assert client.get(HZ_PREFIX + '/auth/status').status_code == 400


def test_same_cookie_has_separate_cipher_namespace_and_shared_revocation(realms):
    primary, alias, _, client = realms
    alias_cookie, _ = alias.create(tokens())
    assert primary.vault.get(alias_cookie) is None
    assert OidcSessions(alias.config, alias.client, realm=hz(alias.config)).vault.get(alias_cookie)
    with alias.vault.connection() as db:
        cipher = db.execute('SELECT value FROM rp_state WHERE id=?', (alias.vault.record_id(alias_cookie),)).fetchone()[0]
        # Copying ciphertext into the canonical namespace cannot make it valid.
        db.execute('INSERT INTO rp_state(id,kind,expires,value) VALUES(?,?,?,?)', (primary.vault.record_id(alias_cookie), 'session', 9999999999, cipher))
    with pytest.raises(VaultUnavailable):
        primary.vault.get(alias_cookie)
    primary.vault.revoke_sid('sid')
    assert alias.vault.get(alias_cookie) is None
    with pytest.raises(VaultUnavailable):
        alias.save(alias_cookie, {'tokens': tokens(), 'identity': {}})


def test_alias_origin_checks_legacy_cookie_and_logout_do_not_touch_old_cookies(realms):
    _, alias, _, client = realms
    cookie, _ = alias.create(tokens())
    client.cookies.set(alias.realm.cookie, cookie)
    assert client.post(HZ_PREFIX + '/auth/logout', headers={**HEADERS, 'origin': 'https://montage.hozonauto.com'}, json={}).status_code == 403
    assert alias.vault.get(cookie) and not alias.vault.get(cookie).get('logout_pending')
    assert client.post(HZ_PREFIX + '/auth/bootstrap', headers={**HEADERS, 'origin': 'https://old-origin.invalid'}, json={'ticket': 't' * 30}).status_code == 401
    for path in ['/auth/session', '/auth/legacy']:
        assert client.post(HZ_PREFIX + path, headers=HEADERS, json={}).status_code == 403
    response = client.post(HZ_PREFIX + '/auth/logout', headers={**HEADERS, 'origin': 'https://ixicai.cn'}, json={})
    assert response.status_code == 200
    cookies = response.headers.get_list('set-cookie')
    assert cookies and all(value.startswith('__Host-hz-dofe_rp_montage') for value in cookies)
    assert all('Domain=' not in value and 'Path=/' in value for value in cookies)


def test_alias_scripts_scope_all_urls_and_no_shared_storage_clear(realms):
    *_, client = realms
    worker = client.get(HZ_PREFIX + '/auth/session-worker.js', headers=HEADERS)
    assert worker.headers['service-worker-allowed'] == HZ_PREFIX + '/'
    assert 'const APP_BASE="/hz/montage"' in worker.text
    assert 'const IDENTITY_ORIGIN="https://ixicai.cn/hz/user/api"' in worker.text
    assert '!url.pathname.startsWith(APP_BASE' in worker.text
    bridge = client.get(HZ_PREFIX + '/auth/ready?next=/../old', headers=HEADERS)
    assert 'data-next="/hz/montage/"' in bridge.text
    assert 'src="/hz/montage/auth/request-bridge.js"' in bridge.text
    for asset in ['request-bridge.js', 'login.js', 'logout-client.js']:
        text = client.get(HZ_PREFIX + '/auth/' + asset, headers=HEADERS).text
        assert 'storage.clear' not in text and 'BroadcastChannel' not in text
    assert client.post(HZ_PREFIX + '/auth/backchannel', headers=HEADERS).status_code == 400


def test_alias_pending_response_loss_recovers_after_worker_restart(realms):
    _, alias, identity, _ = realms
    cookie, _ = alias.create(tokens())
    identity.lost = True
    with pytest.raises(AuthenticationUnavailable):
        alias.prepare(cookie, digest('accepted business'))
    pending = alias.vault.get(cookie)['request_pending']
    restarted = OidcSessions(alias.config, identity, realm=hz(alias.config))
    restarted.prepare(cookie, digest('next business'))
    rotations = [body for path, body in identity.calls if path == 'rotate']
    assert rotations[0]['operation_id'] == rotations[1]['operation_id'] == pending['operation_id']
    assert identity.generation == 2


@pytest.mark.parametrize('path', ['/../old', '/%2e%2e/old', '/%252e%252e/old', '//old', '/%5cold'])
def test_redirect_cannot_escape_alias_mount(path):
    assert local_path(path) == '/'


def test_same_alias_cipher_cannot_move_between_record_or_authority(realms):
    _, alias, _, _ = realms
    alias.vault.put('first', {'secret': 'unit'}, 9999999999)
    with alias.vault.connection() as db:
        cipher = db.execute('SELECT value FROM rp_state WHERE id=?', (alias.vault.record_id('first'),)).fetchone()[0]
        db.execute('INSERT INTO rp_state(id,kind,expires,value) VALUES(?,?,?,?)', (alias.vault.record_id('other'), 'session', 9999999999, cipher))
    with pytest.raises(VaultUnavailable):
        alias.vault.get('other')
    from backlot.session_vault import SessionVault
    wrong_issuer = SessionVault(alias.vault.path, alias.config.session_key, namespace='hz', binding='other-authority')
    with pytest.raises(VaultUnavailable):
        wrong_issuer.get('first')


def test_alias_bootstrap_uses_fixed_exact_target_redirect_not_request_body(realms):
    _, alias, identity, client = realms
    response = client.post(HZ_PREFIX + '/auth/bootstrap', headers={**HEADERS, 'origin': 'https://ixicai.cn'},
                           json={'ticket': 't' * 30, 'target_redirect_uri': 'https://attacker.invalid/', 'realm': 'canonical'})
    assert response.status_code == 200
    exchanges = [body for path, body in identity.calls if path.endswith('bootstrap-exchange')]
    assert exchanges == [{'ticket': 't' * 30, 'target_redirect_uri': HZ_BASE + '/auth/callback'}]
    assert response.cookies.get(alias.realm.cookie)


def test_alias_wrong_realm_target_ticket_is_rejected_without_cookie_or_session(realms):
    _, alias, identity, client = realms
    def rejected(path, body):
        assert body['target_redirect_uri'] == HZ_BASE + '/auth/callback'
        from backlot.oidc_session import AuthenticationExpired
        raise AuthenticationExpired('ticket target does not match')
    identity.operation = rejected
    response = client.post(HZ_PREFIX + '/auth/bootstrap', headers={**HEADERS, 'origin': 'https://ixicai.cn'},
                           json={'ticket': 'old-canonical-ticket-123'})
    assert response.status_code == 401 and not response.cookies
    with alias.vault.connection() as db:
        assert db.execute("SELECT count(*) FROM rp_state WHERE kind='session'").fetchone()[0] == 0


def test_native_host_alias_bootstrap_without_browser_origin_keeps_native_authority(realms):
    _, alias, identity, client = realms
    response = client.post(HZ_PREFIX + '/auth/bootstrap', headers=HEADERS, json={'ticket': 'native-target-ticket-123'})
    assert response.status_code == 200
    saved = alias.vault.get(response.cookies[alias.realm.cookie])
    assert saved['tokens']['session_context'] == 'native'
    assert [body for path, body in identity.calls if path.endswith('bootstrap-exchange')][0]['target_redirect_uri'] == HZ_BASE + '/auth/callback'


def test_real_server_html_assets_and_theme_storage_use_alias_mount(realms, monkeypatch):
    import backlot.oidc_routes as routes
    from backlot.oidc_session import OidcConfig
    from backlot.server import create_app
    primary, alias, identity, _ = realms
    cookie, _ = alias.create(tokens())
    monkeypatch.setenv('OPENMONTAGE_OIDC_ENABLED', 'true')
    monkeypatch.delenv('OPENMONTAGE_BACKLOT_BASE_PATH', raising=False)
    monkeypatch.delenv('OPENMONTAGE_BACKLOT_AUTH_URL', raising=False)
    monkeypatch.setattr(OidcConfig, 'from_environment', classmethod(lambda cls: primary.config))
    monkeypatch.setattr(routes, 'OidcSessions', lambda config, client=None, realm=None: OidcSessions(config, identity, realm=realm))
    client = TestClient(create_app(), base_url=primary.config.public_origin)
    client.cookies.set(alias.realm.cookie, cookie)
    response = client.get(HZ_PREFIX + '/', headers=HEADERS)
    assert response.status_code == 200
    assert '<meta name="backlot-base-path" content="/hz/montage">' in response.text
    assert 'src="/hz/montage/ui/library.js?' in response.text
    assert 'href="/hz/montage/ui/board.css?' in response.text
    for asset in ['library.js', 'lib.js', 'board.css', 'i18n.js']:
        source = client.get(HZ_PREFIX + '/ui/' + asset, headers=HEADERS)
        assert source.status_code == 200
        if asset == 'library.js':
            assert 'hz.montage.backlot.theme' in source.text


def test_duplicate_mount_headers_fail_closed(realms):
    *_, client = realms
    headers = list(HEADERS.items()) + [('x-external-base', HZ_BASE)]
    assert client.get(HZ_PREFIX + '/auth/status', headers=headers).status_code == 400
    assert client.get('/auth/status', headers={'x-external-base': ''}).status_code == 400


def test_canonical_bootstrap_also_binds_its_exact_callback_for_old_and_new_hosts(realms):
    primary, _, identity, client = realms
    response = client.post('/auth/bootstrap', json={'ticket': 'canonical-target-ticket-123'})
    assert response.status_code == 200
    exchanges = [body for path, body in identity.calls if path.endswith('bootstrap-exchange')]
    assert exchanges[0]['target_redirect_uri'] == 'https://montage.hozonauto.com/auth/callback'
    assert response.cookies.get(primary.realm.cookie)
