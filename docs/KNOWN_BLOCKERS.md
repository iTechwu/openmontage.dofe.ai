# Known Blockers

Referenceable tracking for accepted, documented limitations that are **not
closed** and depend on an external capability OpenMontage cannot ship itself.
Each entry has a stable ID referenced from the code (`TRACKED EXTERNAL BLOCKER`)
and is enforced by an automated capability probe so it cannot drift into silence.

---

## KB-001 — Codex Responses same-content wrong-merge

| field | value |
| --- | --- |
| **Status** | OPEN (accepted, documented limitation — not a completed feature) |
| **Owner** | OpenMontage backend maintainer (delegation proxy / worker executor) |
| **External tracker** | **PENDING** — no upstream `openai/codex` issue is known to track this (issue [#1194](https://github.com/openai/codex/issues/1194) is alt-provider auth, not per-call identity). Upstream issues search: <https://github.com/openai/codex/issues?q=is%3Aissue+idempotency>. Owner must **file** an issue requesting *"model-request per-call identity (per-call header interpolation, or a stable per-call `Idempotency-Key` on the Responses request)"* before **Next review**, then set the tracker to FILED with the issue URL here and in `docs/codex_capability_probe.json`. The structured/test-enforced tracker state lives in `docs/codex_capability_probe.json` → `external_tracker`; `test_external_tracker_is_concrete_not_a_placeholder` fails if this stays an unactioned placeholder or if the date lapses. |
| **First verified** | codex-cli 0.146.0 (the `CODEX_CLI_VERSION` Dockerfile pin) |
| **Next review** | On every `CODEX_CLI_VERSION` bump (enforced by `test_codex_capability_probe.py`) **and** no later than 2026-11-07, whichever comes first |
| **Probe** | `tests/openmontage/test_codex_capability_probe.py` + audited manifest `docs/codex_capability_probe.json` |
| **Code reference** | `openmontage/delegation_proxy.py` — `TRACKED EXTERNAL BLOCKER` comment and the content-fingerprint replay fallback |

### Symptom

Without a caller-supplied per-call identity, two genuinely distinct same-content
`/v1/responses` calls within one stage/attempt collapse onto one invocation and
the second replays the first cached response — losing the second call's
execution, billing, and attribution. This is bounded to a single stage/attempt
and rare in practice (Codex rarely issues byte-identical Responses twice in one
stage).

### Why it is open

`DelegationSigningProxy` signs Codex's OpenAI-compatible traffic. Native
OpenMontage tool paths supply a stable logical-call identity
(`X-OpenMontage-Logical-Call-Id`), so the proxy keys replay strictly on it.
Codex cannot: verified against codex-cli 0.146.0 (and re-verified behaviorally —
see the probe in `docs/codex_capability_probe.json`), `ModelProviderInfo` has no
model-request header field at all. `http_headers` belongs to `RawMcpServerConfig`
/ MCP servers and is resolved once executor-side (static-per-process), so
any identity derived from them would collapse every call in a stage onto one id
— strictly worse than the content fingerprint. There is no model-request-level
per-call header or `Idempotency-Key`. The proxy therefore dedups Codex Responses
on the content fingerprint when no logical-call header is present.

### Interim mitigation

Observability only. Every fingerprint-keyed replay is logged at INFO with
`replay_key_source="content_fingerprint"` (the wrong-merge-prone case) versus
`"logical_call_id"` (the safe case), and the CLI configures logging so the
record emits under the default Worker/CLI config.

### Unblock condition

Codex model-provider config or the model request carries a value that **varies
per call**: per-call header interpolation, or a stable per-call `Idempotency-Key`
on the Responses request — **and** a behavioral probe confirms it varies across
two distinct same-stage calls.

The capability probe is a **schema-integrity change detector**, not a behavioral
proof. The shipped native codex binary emits `struct ModelProviderInfo with <N>
elements` (a Rust Debug impl string) — the authoritative structural signature of
what a model-provider config can carry. The audited baseline is `N = 18`
(`docs/codex_capability_probe.json` → `model_provider_info_elements`); the manual
audit established those fields include no per-call header (`http_headers` belongs
to `RawMcpServerConfig` / MCP servers, resolved once executor-side, not to
`ModelProviderInfo`). If a Codex bump changes `N`, the probe fails and forces a
manual behavioral re-audit (run Codex through `DelegationSigningProxy` against a
mock Responses upstream, diff the per-call headers) before this entry can be
touched. A named-substring search is deliberately not used: field names live as
contiguous string-table internings (partial, with false neighbors), so it both
misses unnamed fields and false-positives on unrelated strings.

### When unblocked

Remove the content-fingerprint replay fallback in `delegation_proxy.py`, key all
replay on per-call identity, set `capability_present=true` is **not** sufficient
on its own — close this entry (status → CLOSED) and update the manifest after
the fallback is gone. The probe test fails until that cleanup lands.
