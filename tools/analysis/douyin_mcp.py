"""Prefer MCP-supplied TOS URL download for public Douyin videos, with local fallback.

Thin outer client for the external tools.dofe.ai MCP tool
``viral_video_douyin_tos_url`` as described in the Douyin MCP handoff. It:

- talks MCP Streamable HTTP to ``OPENMONTAGE_DOUYIN_MCP_URL`` using the project's
  already-declared official ``mcp`` client (``streamable_http_client`` +
  ``ClientSession``), which handles the standard ``initialize`` handshake,
  protocol-version header, session lifecycle, and SSE framing automatically;
- calls ``viral_video_douyin_tos_url`` to obtain a pre-signed TOS download URL;
- downloads that URL to ``output_dir/reference_video.mp4`` reusing the local
  streaming download logic (no Douyin-specific UA/Referer headers are needed for
  the pre-signed TOS direct link).

This module is intentionally **non-blocking to the pipeline**: any failure
(unset URL, transport error, isError envelope, parse failure, download failure)
returns ``None`` so callers silently fall back to the local ``DouyinShareClient``.
It never raises an exception that would interrupt the Douyin download flow
(per the agreed non-fail-closed policy).
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

MCP_TOOL_NAME = "viral_video_douyin_tos_url"
MCP_ENV = "OPENMONTAGE_DOUYIN_MCP_URL"

# Explicit MCP transport timeouts (seconds). We don't rely on the SDK's
# 30s / 300s defaults being stable; declare them here.
# The SSE read budget (5 min) must comfortably cover a FIRST-EVER archive call,
# which pays a real Douyin download + TOS upload (~3-4 min) before returning the
# pre-signed URL. Once the object is cached (stat_hit), later calls are
# sub-second.
_MCP_GENERAL_TIMEOUT_SECONDS = 30.0
_MCP_SSE_READ_TIMEOUT_SECONDS = 300.0  # 5 minutes

# 2 GiB, matching DouyinShareClient.download safety limit.
_MAX_DOWNLOAD_BYTES = 2_000 * 1024 * 1024
# A single pre-signed URL is good for the default tools lifetime.
_DEFAULT_EXPIRES_IN = 3600
# Upper bound for replay / retry across the whole MCP exchange.
_MAX_RETRIES = 3
_RETRY_DELAY_SECONDS = 1.0

# Failure codes worth a bounded retry. PROVIDER_ERROR is intentionally NOT
# retried here: it is an opaque Douyin detail-parsing failure that the remote
# tool itself marks non-retryable (`retryable=False`).
#
# Idempotency semantics when retrying:
# - IDEMPOTENCY_IN_PROGRESS  -> the same operation is still being processed on
#   the server; reuse the SAME key so the retry observes the in-flight result.
# - The other retryable codes mean a concrete step already failed; if the server
#   persists and replays a failed attempt under the same key (the handoff says
#   "same key -> replay the first result"), a same-key retry would spin. So for
#   these we retry with a FRESH key.
_RETRYABLE_CODES = frozenset(
    {
        "IDEMPOTENCY_IN_PROGRESS",
        "DOUYIN_VIDEO_DOWNLOAD_FAILED",
        "DOUYIN_TOS_UPLOAD_FAILED",
        "TOS_PRESIGN_FAILED",
    }
)


def _should_retry(error_code: str) -> bool:
    return error_code in _RETRYABLE_CODES


@dataclass
class McpDownload:
    """Successful MCP download: local path plus the archive envelope's identity.

    ``title`` / ``aweme_id`` are best-effort — they come from the success envelope
    (``content.title`` / ``content.awemeId``) and may be empty if the remote tool
    didn't include them. They are NOT a substitute for the local extractor's fuller
    metadata; ``video_downloader`` uses them only to patch the exhaust when the local
    ``DouyinShareClient.extract`` fails but MCP download succeeds.
    """

    path: str = ""
    title: str = ""
    aweme_id: str = ""


async def _call_tool_once(
    base_url: str,
    tool: str,
    arguments: dict[str, Any],
) -> tuple[str | None, bool, str | None]:
    """One full MCP round-trip through the official client.

    Returns ``(text, is_error, error_code)`` where ``text`` is the raw text
    content of the successful/error envelope and ``error_code`` is the stable
    error code string for ``isError`` results (``None`` otherwise). Raises only
    on transport/protocol failures (network, handshake, malformed response);
    the caller treats those as a soft failure -> local fallback.
    """
    http_client = create_mcp_http_client(
        timeout=httpx2.Timeout(
            _MCP_GENERAL_TIMEOUT_SECONDS, read=_MCP_SSE_READ_TIMEOUT_SECONDS
        )
    )
    transport = streamable_http_client(base_url, http_client=http_client)
    async with http_client:
        async with transport as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                client_info=types.Implementation(
                    name="openmontage",
                    version="0.1.0",
                ),
            ) as session:
                # The protocol version (2025-06-18, negotiable) is handled by the
                # SDK's own initialize() — ClientSession does NOT accept a
                # protocol_version kwarg and its __aenter__ does not handshake,
                # so this explicit initialize is required before call_tool.
                await session.initialize()
                result = await session.call_tool(tool, arguments=arguments)

    if isinstance(result, types.CallToolResult):
        is_error = bool(result.is_error)
        text: str | None = None
        error_text: str | None = None
        for item in result.content or []:
            if isinstance(item, types.TextContent):
                text = item.text or ""
                break
        if is_error and text:
            try:
                parsed = json.loads(text)
                err = (parsed or {}).get("error") or {}
                error_text = err.get("code") or err.get("message") or text
            except (ValueError, AttributeError, TypeError):
                error_text = text
        return text, is_error, error_text

    # Unexpected non-CallToolResult result (e.g. InputRequired) -> soft failure.
    return None, False, None


async def _resolve_tos_url(
    base_url: str, raw_url: str
) -> dict[str, str] | None:
    """Retry loop around ``_call_tool_once``.

    Returns ``{"tos_url", "title", "aweme_id"}`` on success, ``None`` on failure
    (so callers silently fall back to the local ``DouyinShareClient``)."""
    base_key = f"openmontage-{_uuid4_hex()}"
    last_code: str | None = None
    for attempt in range(_MAX_RETRIES):
        # IDEMPOTENCY_IN_PROGRESS must reuse the SAME key as the original call
        # (the attempt that is still in flight), so we keep the bare base key.
        # A concrete failure (download/upload/presign) mints a FRESH key so a
        # server that persists and replays by key does not spin on the failed
        # attempt.
        key = base_key
        if last_code and last_code != "IDEMPOTENCY_IN_PROGRESS":
            key = f"{base_key}-{attempt}"
        arguments: dict[str, Any] = {
            "douyinVideoUrl": raw_url,
            "idempotencyKey": key,
            "expiresIn": _DEFAULT_EXPIRES_IN,
        }
        text, is_error, error_code = await _call_tool_once(
            base_url, MCP_TOOL_NAME, arguments
        )
        if is_error:
            last_code = error_code or "UNKNOWN"
            if attempt + 1 < _MAX_RETRIES and _should_retry(last_code):
                _sleep(_RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            return None
        if text is None:
            return None
        envelope = _parse_tos_envelope(text)
        if envelope:
            return envelope
    return None


def _parse_tos_url(content: str) -> str | None:
    """Parse the success envelope and return the pre-signed ``tosUrl``."""
    envelope = _parse_tos_envelope(content)
    return envelope.get("tos_url") if envelope else None


def _parse_tos_envelope(content: str) -> dict[str, str] | None:
    """Best-effort extract of the success envelope: pre-signed URL + identity.

    Returns ``None`` when the envelope isn't valid/has no usable URL; otherwise
    ``{"tos_url", "title", "aweme_id"}``. ``title``/``aweme_id`` are best-effort
    and default to empty strings — the thin MCP tool may not always return them.
    """
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    url = data.get("tosUrl") or data.get("downloadUrl")
    if not (isinstance(url, str) and url):
        return None
    title = data.get("title")
    aweme_id = data.get("awemeId")
    return {
        "tos_url": url,
        "title": title if isinstance(title, str) else "",
        "aweme_id": str(aweme_id) if aweme_id else "",
    }


def _download_url(
    session: requests.Session,
    url: str,
    output_path: Path,
) -> str:
    """Stream a plain HTTP GET (pre-signed TOS URL) to ``output_path``.

    The pre-signed URL is plain HTTP(S) (volces.com), so no Douyin UA/Referer
    headers are needed. ``requests`` is used here — separate from the MCP
    transport — because this is a raw object-storage download, not an MCP call.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with session.get(
        url,
        stream=True,
        allow_redirects=True,
        timeout=(10, 120),
    ) as response:
        response.raise_for_status()
        length = int(response.headers.get("content-length") or 0)
        if length and length > _MAX_DOWNLOAD_BYTES:
            raise ValueError("Douyin video exceeds the 2000 MB download safety limit")
        written = 0
        with output_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > _MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        "Douyin video exceeds the 2000 MB download safety limit"
                    )
                handle.write(chunk)
    return str(output_path)


def _run_coro(coro):
    """Run an async coroutine from either a sync or an async caller context.

    ``download_via_mcp`` is a sync entry point, but it may be reached from two
    very different callers:

    - a plain sync path (CLI / worker / unit test): no event loop is running,
      so ``asyncio.run`` is correct;
    - an async service (OpenMontage MCP Streamable HTTP server, which handles
      concurrent tool calls from many users on an asyncio event loop): calling
      ``asyncio.run`` here would raise ``RuntimeError`` ("asyncio.run() cannot be
      called from a running event loop") because a loop is already active. In that
      case the coroutine is run on its own loop in a fresh worker thread and the
      result is returned to the caller, so the MCP path keeps working instead of
      silently falling back to the local client.

    Any exception raised by the coroutine propagates to the caller; the
    non-fail-closed guarantee is enforced by ``download_via_mcp``'s outer
    try/except, which reverts to ``None`` -> local fallback.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (sync caller) -> safe to create one here.
        return asyncio.run(coro)
    # A loop is already running (async service caller). Run the coroutine on a
    # fresh loop in a worker thread and block for its result without nesting on
    # the current loop. The new loop is always closed by asyncio.run().
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


def download_via_mcp(raw_url: str, output_dir: str | Path) -> McpDownload | None:
    """Preferred path: MCP -> pre-signed TOS URL -> download ``reference_video.mp4``.

    Returns an :class:`McpDownload` (local path + best-effort title/aweme_id) on
    success, ``None`` on any failure. Never raises. Despite the sync signature, it
    is safe to call from both sync and async (MCP server) contexts; see ``_run_coro``.
    """
    env_url = os.environ.get(MCP_ENV, "").strip()
    if not env_url:
        return None  # MCP not configured -> caller falls back to local client.

    try:
        base_url = env_url.rstrip("/")
        output_path = Path(output_dir) / "reference_video.mp4"
        envelope = _run_coro(_resolve_tos_url(base_url, raw_url))
        if not envelope:
            return None
        # NOTE: the success envelope's ``cached`` field (whether the object was
        # reused from a prior archive) is intentionally not used here. Each call
        # mints a fresh idempotency key for correctness-first semantics, so we
        # never assume a pre-existing archive and always download the returned
        # URL. A future cost optimization could introduce deterministic keys and
        # surface ``cached``, at the cost of expiring ``expiresAt`` handling.
        path = _download_url(requests.Session(), envelope["tos_url"], output_path)
        return McpDownload(
            path=path,
            title=envelope.get("title", ""),
            aweme_id=envelope.get("aweme_id", ""),
        )
    except Exception:  # noqa: BLE001 - non-fail-closed by design.
        return None


def _uuid4_hex() -> str:
    import uuid

    return uuid.uuid4().hex


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)