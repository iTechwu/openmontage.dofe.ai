# Seedance Lineage Review

Use this reviewer extension at `assets` whenever the selected model family is
Seedance. It validates relationships across `scene_plan` and `asset_manifest`;
JSON Schema alone cannot establish these graph-wide facts.

## Evidence Set

Load the current `scene_plan`, `asset_manifest`, every Seedance
`prompt_review.provider_preflight`, every `take_review`, and the current
checkpoint history. Review only recorded facts. If media or state cannot be
inspected, record an `investigation` with the uncertainty instead of inventing
an observation.

## Build The Graph Inventory

1. List every Seedance asset ID exactly once in
   `lineage_review.reviewed_asset_ids`.
2. Record every root asset in `lineage_review.roots`.
3. For every connected scene, create one edge with parent/child asset and scene
   IDs plus the declared continuation relationship.
4. Use the accepted `take_review.observed_end_state` as the parent's transient
   authority. Planned scene text is never evidence that the handoff occurred.

## Mandatory Checks

Record a concrete asset/scene-ID-based evidence sentence for every check:

- `unique_ids`: no duplicate asset IDs or ambiguous take authority.
- `parent_exists`: every parent is present, is not self, and resolves to one
  current take.
- `parent_precedes_child`: parent scene/take occurs before its child; a future
  or sibling take cannot parent an earlier clip.
- `acyclic`: walk each parent chain to a root; no asset may be revisited.
- `accepted_parent_authority`: each parent is `accepted` or
  `accepted_with_deviation`, has `accepted_as_canon=true`, and carries an actual
  observed end state.
- `observed_state_handoff`: the child's `observed_start_state` and prompt use
  the accepted parent's actual visual, motion, camera, light, prop, damage, and
  audio state. Record meaningful deviations.
- `extension_depth_and_reanchor`: output-sourced depth advances consistently;
  re-anchor resets to canonical references at depth zero; depth three does not
  silently continue.
- `beat_and_identity_continuity`: completed beats do not replay, reserved beats
  do not leak, and identity/geography/prop ownership changes are either locked
  or explicitly approved. This is a reviewer judgment, not a Python rule.
- `reference_binding_matches_preflight`: every scene reference role uses a mode
  supported by its stored provider preflight. Prompt tokens require an exact
  declared provider syntax; otherwise the binding must be an input parameter.

Use `not_applicable` only when the graph genuinely has no relevant edge or
reference. The evidence must say why.

## Findings And Decision

- Missing parents, cycles, future-parent edges, rejected parents, invented
  observed state, or unsupported reference binding are `critical` with an exact
  `proposed_fix`.
- Identity, geography, beat, or state drift with a usable corrective path is a
  `suggestion` unless it breaks the approved delivery promise or makes the next
  continuation unsafe; then it is `critical`.
- `pass` requires every structural check to pass or be honestly not applicable,
  no pending critical finding, and evidence for every check.
- `revise` when a critical finding remains. Rebuild the affected edge or
  re-anchor, then run a second review round.
- After two rounds, use `pass_with_warnings` only under the main reviewer
  protocol. Never rewrite a failed graph check as passed to move forward.

Persist the inventory, checks, findings, and decision in
`asset_manifest.lineage_review`. Python and Schema validate record structure;
the agent reviewer owns graph interpretation and creative continuity judgment.
