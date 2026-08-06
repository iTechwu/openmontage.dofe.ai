---
name: seedance-quality
description: Review Seedance prompts and generated takes for controllability, identity, continuity, motion, camera, dialogue, audio, text, and endpoint failures. Use before paid generation, during sample approval, after every connected clip, and when choosing keep, post-fix, re-roll, rewrite, or re-anchor.
---

# Seedance Quality

Separate provider failures, prompt failures, and take failures. Repair one
variable at a time so the next result teaches something.

## Prompt Preflight

Reject or revise a prompt when any of these is true:

- provider/mode capabilities were assumed rather than verified;
- the shot has no completed endpoint;
- several actions compete inside one short shot;
- identity, motion, and scene density are all maximal;
- camera terms conflict or the move has no narrative/utility purpose;
- reference roles overlap or omit a non-transfer clause;
- a continuation uses planned state instead of accepted observed state;
- completed beats replay or reserved beats appear early;
- dialogue lacks a visible speaker, stable screen position, reaction, or pause;
- a recurring subject has no registry ID or the prompt conflicts with its
  canonical anchor;
- temporal beats are unordered, duplicated, or lack completed states;
- abstract adjectives replace filmable behavior, light, or sound;
- required text, plate, subtitle, or logo rendering is delegated to the model.

Store the draft, critique, final prompt, skills applied, continuity/reference
checks, and provider-neutral `compile_spec` in
`asset_manifest.assets[].prompt_review`. Verify surface profile, section order,
reference emissions, carrier coverage, compression decisions, and endpoint.

## Take Review

Inspect the returned media and choose exactly one outcome:

- `keep`: the take meets the shot contract and can enter canon.
- `post_fix`: picture is usable and the defect is safer to fix in compose, such
  as plate text, subtitle, mix level, color, or a removable tail.
- `reroll`: prompt is sound; retry the same contract with one controlled change.
- `rewrite`: the prompt caused ambiguity, overload, or conflict.
- `reanchor`: identity or continuity has drifted; restart from canonical refs.
- `reject`: the take cannot be used and must not enter sequence canon.

Read [references/retake.md](references/retake.md) before spending on a retry.

## Review Dimensions

- story/utility endpoint and felt intent;
- subject and vehicle identity;
- pose, screen direction, geography, props, damage, and environment continuity;
- action causality, weight, timing, and motion completion;
- requested framing, camera move, focus, and light source;
- dialogue ownership, mouth sync, turn-taking, ambience, and SFX sync;
- unintended text/logos, deformation, flicker, duplication, or black frames;
- duration, resolution, and technical validity.

For every scene `identity_id`, write one `take_review.identity_observations`
record with evidence and `preserved`, `deviated`, or `unverifiable`. Do not hide
identity drift inside a generic issue string. Record the returned take's
subjects, props, environment, camera, lighting, audio, and open motion in
`take_review.observed_state`; uncertainty belongs in `uncertainties`, not in a
fabricated value.

## Sequence Acceptance

Only `keep` or an explicitly accepted deviation updates canon. Record the
observed end state rather than copying the planned endpoint. Preserve the old
plan in history and use the observed state for the next generation.

Persist the outcome, issues, canon decision, observed end state, and next action
in `asset_manifest.assets[].take_review`. A `keep` decision must set
`accepted_as_canon=true` and `canon_status=accepted`; reroll, rewrite, re-anchor,
and reject decisions must remain `not_accepted`. An accepted deviation uses
`canon_status=accepted_with_deviation` and records the actual endpoint. Every
accepted take also records the seven-dimensional `observed_state`; its summary
alone is not sufficient for a continuation handoff.

After all current takes are recorded, run
`skills/meta/seedance-lineage-review.md`. Do not infer that individually valid
take reviews form a valid sequence: the reviewer must inventory roots and edges,
walk parent chains, compare observed handoffs, and record the result in
`asset_manifest.lineage_review`.

Derived from MIT-licensed
[`Emily2040/seedance-2.0`](https://github.com/Emily2040/seedance-2.0); license is
retained at `../seedance-provider/LICENSE`.
