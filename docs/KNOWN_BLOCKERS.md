# Known Blockers

Referenceable tracking for externally-gated items: open limitations **and**
mitigated entries whose residual simplification still depends on a capability
OpenMontage cannot ship itself. Each entry has a stable ID referenced from the
code (`KB-001`) and is enforced by an automated capability probe so it cannot
drift into silence.

---

## KB-001 — Codex Responses same-content wrong-merge

| field | value |
| --- | --- |
| **Status** | MITIGATED — the proxy combines the durable content fingerprint with a stable occurrence ordinal. Concurrent and sequential distinct calls receive different invocation IDs, while a deterministic stage restart replays each occurrence in order. A native Codex per-call identity would remove the remaining order-dependence. |
| **Owner** | OpenMontage backend maintainer (delegation proxy / worker executor) |
| **External tracker** | **PENDING** — no upstream `openai/codex` issue is known to track this (issue [#1194](https://github.com/openai/codex/issues/1194) is alt-provider auth, not per-call identity). Upstream issues search: <https://github.com/openai/codex/issues?q=is%3Aissue+idempotency>. Owner must **file** `external_tracker.issue_draft` (verbatim or adapted) before **Next review**, then set the tracker to FILED with the issue URL here and in `docs/codex_capability_probe.json`. The structured/test-enforced tracker state lives in `docs/codex_capability_probe.json` → `external_tracker`; `test_external_tracker_is_concrete_not_a_placeholder` fails if PENDING lacks a ready-to-file `issue_draft` (title + body) or if the review date lapses, and `test_review_deadline_window_bounds_probe_recency` fails if the deadline is pushed more than a quarter past the last probe. |
| **First verified** | codex-cli 0.146.0 (the `CODEX_CLI_VERSION` Dockerfile pin) |
| **Next review** | On every `CODEX_CLI_VERSION` bump (enforced by `test_codex_capability_probe.py`) **and** no later than 2026-11-07, whichever comes first |
| **Probe** | `tests/openmontage/test_codex_capability_probe.py` + audited manifest `docs/codex_capability_probe.json` |
| **Code reference** | `openmontage/delegation_proxy.py` — `KB-001` comment and the content-fingerprint occurrence sequence |

### Symptom (before mitigation)

Without a caller-supplied per-call identity, two genuinely distinct same-content
`/v1/responses` calls within one stage/attempt would collapse onto one
invocation and the second would replay the first cached response — losing the
second call's execution, billing, and attribution. Bounded to a single
stage/attempt.

### How it is mitigated

When no logical-call header is present, `DelegationSigningProxy` atomically
assigns each occurrence of a content fingerprint an ordinal. Occurrence 1 keeps
the legacy fingerprint request ID; later calls use
`<fingerprint>::occurrence::<n>`:

- sequential and concurrent independent same-content calls receive different,
  deterministic invocation IDs;
- a restarted deterministic stage starts again at occurrence 1 and replays each
  persisted occurrence in order, including the second and later calls;
- a failed tail occurrence rolls back the ordinal, so a sequential retry reuses
  the same invocation ID instead of splitting billing and attribution.

Responses SSE completion is parsed structurally. Only an
`event: response.completed`, a JSON data object whose `type` is
`response.completed`, or the exact `data: [DONE]` sentinel is terminal. EOF
without one is failed and not cached; model text containing the words
`response.completed` cannot falsely commit a truncated stream.

Callers that can supply a stable identity (native tool paths) set
`X-OpenMontage-Logical-Call-Id`, used strictly with no fallback. Without that
identity, recovery assumes same-content calls occur in the same order when a
stage restarts. Identical bytes cannot reveal whether reordered or hedged calls
are retries or distinct calls; native per-call identity removes that residual.

Every fingerprint-keyed replay is also logged at INFO with
`replay_key_source="content_fingerprint"` (vs `"logical_call_id"`), and the CLI
configures logging so the record emits under the default Worker/CLI config.

### Why a native per-call identity still helps

Codex cannot supply a per-call identity: verified against codex-cli 0.146.0 (and
re-verified behaviorally — see the probe in `docs/codex_capability_probe.json`),
`ModelProviderInfo` has no model-request header field at all. `http_headers`
belongs to `RawMcpServerConfig` / MCP servers and is resolved once executor-side
(static-per-process), so any identity derived from it would collapse every call
in a stage onto one id. There is no model-request-level per-call header or
`Idempotency-Key`. The occurrence sequence closes deterministic sequential,
concurrent, and restart recovery paths without that capability. A native Codex
per-call identity would let replay key strictly on the call itself and remove
the remaining call-order assumption. The capability probe tracks when that
arrives.

### Capability probe

The probe is **behavioral**, not a struct element-count. It runs the pinned codex
binary non-interactively (`codex exec`) with a dummy credential against a
loopback mock OpenAI Responses upstream that records every request, and observes
the ACTUAL wire surface Codex sends on `/responses`: the sorted **set** of
request header names and request body field names (excluding the per-request
transport headers `host` and `content-length`). The audited baseline is in
`docs/codex_capability_probe.json` → `behavioral_probe_baseline`. If a Codex bump
adds or removes a name, the probe fails and forces a manual audit — a new
header/field may carry per-call identity; if it does, remove the guard and the
content-fingerprint fallback and close this entry; if it does not, update the
baseline after auditing.

Behavioral, not element-count: a request-level identity header would never
change `ModelProviderInfo`'s element count, so element-count is blind to the
exact signal; it also trips on unrelated struct changes and misses add+delete
pairs. Element-count is no longer used. Per-call *variation* (an existing field
gaining per-call semantics without a name change) is not machine-detectable from
a name set; it is covered by the manual audit every pin bump triggers, since the
strict CI job re-runs this probe against the new binary.

### When a native per-call identity arrives

Remove the content-fingerprint occurrence fallback in `delegation_proxy.py`,
key all replay on per-call identity, set
`capability_present=true`, close this entry (status → CLOSED), and update the
manifest. The probe test `test_fingerprint_fallback_presence_matches_recorded_capability`
fails until that cleanup lands.
