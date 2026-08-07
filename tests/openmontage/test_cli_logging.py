"""CLI logging configuration: the delegation replay audit records must be
observable under the default Worker/CLI configuration, not only when a test
temporarily forces INFO via caplog.
"""

from __future__ import annotations

import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests  # noqa: E402

from openmontage import cli  # noqa: E402
from openmontage.delegation_proxy import DelegationSigningProxy  # noqa: E402
from openmontage.invocation_store import ModelInvocationStore  # noqa: E402
from tools.dofe.delegation import DelegatedModelCredential  # noqa: E402


def test_cli_entry_enables_info_logging_by_default(capsys) -> None:
    """main() configures logging so delegation_proxy INFO is not filtered out.

    The handler is scoped to the ``openmontage`` namespace (NOT root), so the
    openmontage logger is raised to INFO while the root logger stays at its
    default WARNING — third-party library records never pass through this handler.
    """
    rc = cli.main(["capabilities", "--json"])
    assert rc == 0
    capsys.readouterr()  # drain capability output

    proxy_logger = logging.getLogger("openmontage.delegation_proxy")
    assert proxy_logger.isEnabledFor(logging.INFO), (
        "delegation proxy INFO must be enabled after CLI startup — otherwise "
        "wrong-merge replay records are silent under the default configuration"
    )
    openmontage_logger = logging.getLogger("openmontage")
    assert any(
        getattr(handler, "_openmontage_logging", False)
        for handler in openmontage_logger.handlers
    ), "CLI must attach the OpenMontage stderr handler to the openmontage logger"
    assert openmontage_logger.propagate is False, (
        "openmontage must not propagate to root, or third-party records sharing "
        "root would leak through the OpenMontage handler"
    )
    root = logging.getLogger()
    assert not any(
        getattr(handler, "_openmontage_logging", False) for handler in root.handlers
    ), "handler must live on the openmontage namespace, not root"
    assert not root.isEnabledFor(logging.INFO), (
        "root must stay at WARNING so third-party INFO records are filtered"
    )


def test_third_party_logger_credentials_are_not_emitted(capsys) -> None:
    """A third-party logger's credential-bearing INFO record must NOT reach stderr.

    Guards the credential-leak regression: the OpenMontage handler is scoped to
    the openmontage namespace and root stays at WARNING, so an httpx/requests-
    style logger emitting api_key/authorization at INFO is filtered out and never
    formatted (the whitelist formatter is a second guard, but the record must not
    even reach it).
    """
    cli._configure_logging("INFO")
    capsys.readouterr()  # drain any startup output

    third_party = logging.getLogger("httpx")
    third_party.info(
        "sending request with Authorization: Bearer sk-super-secret",
        extra={"api_key": "sk-super-secret", "authorization": "Bearer sk-super-secret"},
    )
    err = capsys.readouterr().err

    assert "sk-super-secret" not in err
    assert "api_key" not in err and "authorization" not in err
    assert "httpx" not in err


def _echo_upstream(forwarded: list) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            forwarded.append(
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
            )
            body = b'{"id":"resp-1","output":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:  # noqa: N802
            return None

    return Handler


def test_fingerprint_replay_emits_to_stderr_under_default_config(
    tmp_path, capsys
) -> None:
    """A fingerprint-keyed replay emits a structured record to stderr under the
    CLI's default INFO config — proving the wrong-merge risk is observable in
    production without caplog temporarily forcing the level."""
    cli._configure_logging("INFO")
    forwarded: list = []

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _echo_upstream(forwarded))
    Thread(target=upstream.serve_forever, daemon=True).start()
    try:
        host, port = upstream.server_address
        credential = DelegatedModelCredential(
            api_key="delegated-api-key",
            models_base_url=f"http://{host}:{port}/api",
            delegation_id="delegation-1",
            external_job_id="job-cli-log-emit",
            pipeline_stage="idea",
            runtime_credential_id="runtime-credential-1",
            expires_at="2099-08-06T09:00:01Z",
        )
        store = ModelInvocationStore(tmp_path / "jobs.sqlite3")
        payload = {"model": "catalog-model", "input": "same assignment"}
        with DelegationSigningProxy(
            credential, invocation_store=store, stage_attempt=1
        ) as proxy:
            requests.post(f"{proxy.base_url}/v1/responses", json=payload, timeout=5)
            capsys.readouterr()  # drain the first (non-replay) call
            requests.post(f"{proxy.base_url}/v1/responses", json=payload, timeout=5)
        err = capsys.readouterr().err
    finally:
        upstream.shutdown()
        upstream.server_close()

    assert "openmontage.delegation_proxy" in err
    assert "replay_served" in err  # the structured event field
    assert "replay_key_source=" in err and "content_fingerprint" in err
