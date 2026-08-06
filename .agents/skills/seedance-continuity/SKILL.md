---
name: seedance-continuity
description: Plan and control connected Seedance clips, character or vehicle identity, reference authority, accepted-state handoffs, scene boundaries, re-anchors, completed beats, and reserved future beats. Use for series, long stories, continuations, retakes, or any multi-generation scene.
---

# Seedance Continuity

Plan the whole sequence, but finalize only the next unresolved generation.
Accepted footage is canon; the original plan is not.

For a new or resumed active sequence, use
`seedance_contract_version="2.0"`. Read the provider
[contract-versioning reference](../seedance-provider/references/contract-versioning.md)
before upgrading a legacy checkpoint; preserve the old artifact and derive
every new handoff from inspected evidence.

## Sequence Workflow

1. Establish the story objective, final outcome, ordered beats, scene map, and
   canonical identity/reference registry before generating Clip 01.
   Store the identity bible once in `scene_plan.identity_registry`; scenes use
   `identity_ids` and never fork their own descriptions.
2. Group clips into scenes with one location and time envelope. Use seamless
   continuation only inside a scene.
3. Give each clip one narrative job, one felt intent, one planned start, and one
   completed endpoint. Mark completed and reserved beats separately.
4. Attach [seedance-directing](../seedance-directing/SKILL.md) shot contracts to
   the current clip. Future clips remain provisional intent cards until their
   predecessor is accepted.
5. After generation, inspect the clip or its final frame, record the observed
   subjects, props, environment, camera, lighting, audio, and open motion, then
   update canon before compiling the next prompt.
6. Re-anchor from canonical references after two consecutive output-sourced
   generations by default; never exceed three. Re-anchor immediately when
   identity, geography, or motion drifts.

## Canon Rules

- Only `accepted` or `accepted_with_deviation` footage can become a parent.
- Rejected footage never updates canon.
- Observed accepted state overrides planned state.
- A scene boundary is an intentional cut from canonical references and resets
  extension depth.
- Completed actions must not replay. Reserved future actions must not leak into
  the current generation.
- Preserve reference tags byte-for-byte. Never renumber or normalize them.

## Identity Lock

For every recurring subject, maintain a compact identity anchor:

- stable ID and role;
- silhouette/body class and proportions;
- face or anthropomorphic feature placement;
- primary colors, materials, wardrobe/accessories;
- persistent marks, damage, and prop ownership;
- attributes intentionally allowed to change.

The registry entry is canonical. Per-scene `identity_anchors`, prompt locks,
and `take_review.identity_observations` must all resolve to the same stable ID.
Record a deviation; never silently edit the registry to match a failed take.

For vehicles, include model class, body silhouette, front/rear light signature,
wheel design, paint, windows, face placement, damage state, and plate overlay
zone. Required plate or logo text remains a post-production responsibility.

## Reference Authority

Read [references/reference-authority.md](references/reference-authority.md) when
references are present. Assign one winning reference per controlled dimension:
identity, first frame, last frame, product, environment, motion, camera, timing,
or audio. A reference with no owned dimension should be removed.

## Handoff Contract

Read [references/sequence-state.md](references/sequence-state.md) before a
continuation. Record the seven required state dimensions in both the accepted
take's `observed_state` and the child's `continuity_state.handoff_state`. The
two records describe the same observed boundary. If the media cannot be
inspected, mark the state as user-reported and low-confidence; never invent
observations.

Persist per-scene controls in `scene_plan.scenes[].generation_contract` and
persist accepted-take review in `asset_manifest.assets[].take_review`. Keep
`extension_depth`, source status, observation confidence, uncertainties, and
re-anchor decisions synchronized across both artifacts.

Before the assets checkpoint, run the OpenMontage reviewer extension at
`skills/meta/seedance-lineage-review.md` and persist its graph inventory in
`asset_manifest.lineage_review`. Schema validation is record-local and never
replaces the reviewer checks for parent existence/order, cycles, accepted-state
authority, beat leakage, identity-registry agreement, prompt compilation trace,
or reference/preflight agreement.

Derived from MIT-licensed
[`Emily2040/seedance-2.0`](https://github.com/Emily2040/seedance-2.0); license is
retained at `../seedance-provider/LICENSE`.
