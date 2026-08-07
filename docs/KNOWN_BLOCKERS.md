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
| **Status** | MITIGATED — the same-instance wrong-merge is closed in OpenMontage by the per-instance distinct-call guard (`_locally_served` in `delegation_proxy.py`). The content-fingerprint fallback remains the durable cross-instance replay key; a native Codex per-call identity would remove the need for the guard (residual tracked enhancement). |
| **Owner** | OpenMontage backend maintainer (delegation proxy / worker executor) |
| **External tracker** | **PENDING** — no upstream `openai/codex` issue is known to track this (issue [#1194](https://github.com/openai/codex/issues/1194) is alt-provider auth, not per-call identity). Upstream issues search: <https://github.com/openai/codex/issues?q=is%3Aissue+idempotency>. Owner must **file** `external_tracker.issue_draft` (verbatim or adapted) before **Next review**, then set the tracker to FILED with the issue URL here and in `docs/codex_capability_probe.json`. The structured/test-enforced tracker state lives in `docs/codex_capability_probe.json` → `external_tracker`; `test_external_tracker_is_concrete_not_a_placeholder` fails if PENDING lacks a ready-to-file `issue_draft` (title + body) or if the review date lapses, and `test_review_deadline_window_bounds_probe_recency` fails if the deadline is pushed more than a quarter past the last probe. |
| **First verified** | codex-cli 0.146.0 (the `CODEX_CLI_VERSION` Dockerfile pin) |
| **Next review** | On every `CODEX_CLI_VERSION` bump (enforced by `test_codex_capability_probe.py`) **and** no later than 2026-11-07, whichever comes first |
| **Probe** | `tests/openmontage/test_codex_capability_probe.py` + audited manifest `docs/codex_capability_probe.json` |
| **Code reference** | `openmontage/delegation_proxy.py` — `KB-001` comment, the content-fingerprint replay fallback, and the per-instance distinct-call guard |

### Symptom (before mitigation)

Without a caller-supplied per-call identity, two genuinely distinct same-content
`/v1/responses` calls within one stage/attempt would collapse onto one
invocation and the second would replay the first cached response — losing the
second call's execution, billing, and attribution. Bounded to a single
stage/attempt.

### How it is mitigated

`DelegationSigningProxy` tracks, per live instance, the content fingerprints it
has already served (`_locally_served`). When no logical-call header is present:

- a same-content call re-arriving **sequentially within ONE live instance** is
  treated as a distinct call — forwarded again with its own invocation id, never
  collapsed onto the first (this is the case the guard closes);
- a same-content call re-arriving in a **NEW instance** (worker restart) still
  replays the persisted response from the durable ledger — recovery, no re-bill —
  because each instance starts with an empty `_locally_served`;
- **concurrent in-flight** retries still dedup on the content fingerprint — the
  sibling has already committed to the shared seed before `_locally_served` is
  marked at serve completion.

`_locally_served` is marked **only on a committed success** — a cached
successful response (or a replay of one). A **failed** forward (upstream error,
exception, or a **truncated SSE stream** that closed without its
`response.completed` / `[DONE]` terminal marker) marks nothing, so an
in-instance retry of that same logical call reuses the same content-keyed seed
and the same invocation id (no double-billing, no split attribution). A
truncated stream is also marked `failed` (not cached), so a restart recovers by
re-forwarding instead of replaying a broken response forever.

Callers that can supply a stable identity (native tool paths) set
`X-OpenMontage-Logical-Call-Id`, used strictly with no fallback. Residual edges:

- **concurrent**: two **concurrent** same-content arrivals AFTER the instance
  already served that content both forward (each gets a distinct seed) — the
  conservative choice (prefer a possible double-bill over any wrong-merge), far
  rarer than the sequential distinct calls the guard now handles;
- **restart**: within one instance the 2nd..Nth distinct same-content call gets
  a random `::distinct::` uuid seed that cannot be re-derived after a restart,
  so crash-restart replays the 1st such call from the ledger but **re-forwards**
  the 2nd..Nth (a re-bill for those). No caller re-supplies that random seed.

Both residuals are inherent to having no native per-call identity; a native
Codex per-call identity removes the guard (and both residuals) entirely.

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
`Idempotency-Key`. The per-instance guard closes the same-instance wrong-merge
without that capability, but it is a heuristic; a native Codex per-call identity
would let replay key strictly on per-call identity and remove the guard (and its
concurrent-after-complete residual edge) entirely. The capability probe tracks
when that arrives.

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

Remove the `_locally_served` guard AND the content-fingerprint replay fallback in
`delegation_proxy.py`, key all replay on per-call identity, set
`capability_present=true`, close this entry (status → CLOSED), and update the
manifest. The probe test `test_fingerprint_fallback_presence_matches_recorded_capability`
fails until that cleanup lands.
