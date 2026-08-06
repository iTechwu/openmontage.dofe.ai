# Seedance Production Route

Use this Layer 2 route whenever `video_selector` ranks or selects a tool whose
`required_agent_skills` includes `seedance-provider`. It applies to every
pipeline, not only cinematic.

## Before Scene Planning

For all new or resumed Seedance work, use `seedance_contract_version="2.0"` on
the scene generation contract, generated asset, and lineage review. A missing
marker is readable legacy v1; follow the provider skill's contract-versioning
reference before editing it or making another paid call.

1. Call `video_selector` with `operation="rank"` using a brief draft prompt and
   the intended operation/model constraints. Do not spend from rank mode.
2. Inspect the finalist's input-aware `agent_skills`. Read all declared Layer 3
   skills before writing the provider prompt.
3. Announce and lock the exact tool, provider, model, operation, sample/batch
   status, and native-audio choice in the decision log.
4. Before the paid call, run `video_selector` again with
   `operation="preflight"`, the real `target_operation`, locked parameters,
   references, and scene `reference_roles`. Store the returned
   `provider_preflight` with the prompt audit.

## Scene Contract

Before authoring Seedance scenes, create `scene_plan.identity_registry` once for
all recurring characters, vehicles, products, controlled props, and persistent
environments. Treat it as the identity bible; scenes reference `identity_ids`
instead of authoring parallel descriptions.

For every selected Seedance scene, set
`generation_contract.model_family="seedance"` and complete the schema's
`seedance_contract`:

- choose the `narrative` or `utility` lane;
- persist the complete lane-specific authoring state;
- specify one primary action, framing, lens, blocking, camera axis, screen
  direction, motivated light, observable behavior, and sound intent;
- turn the action into ordered `temporal_beats`; for dialogue, persist speaker,
  screen position, eyeline, reaction, and pause as `dialogue_beats`;
- compile internal story choices into concrete `prompt_carriers`;
- record exclusions, accepted source status, the seven-dimensional
  `continuity_state.handoff_state` when available, extension depth, confidence,
  and uncertainties.

Reference tokens are surface-specific. Set `binding_mode=prompt_token` only
when the selected tool contract declares the syntax and record that exact token
in `reference_roles[].tag`. Otherwise use `input_parameter` and a stable internal
tag; never assume `@Image1` is portable across providers.

## Asset Generation

1. Compile only the current scene from `generation_contract`; future scene
   prompts remain provisional.
2. Run `seedance-quality` preflight. Build `prompt_review.compile_spec` before
   final prose, then store draft, critique, final prompt, skills,
   `carrier_coverage`, `compression_decisions`, and provider preflight together.
3. Require provider preflight to be non-blocking. Generate one representative
   sample before a batch. A degraded live probe may support the sample, but the
   batch requires explicit approval recorded as `allow_degraded_preflight=true`.
4. Inspect the actual media, set `model_family="seedance"`, and write a
   schema-valid `take_review` with one observation per identity ID plus subjects,
   props, environment, camera, lighting, audio, and open motion.
5. Only accepted observed state may seed the next connected clip. A rejected,
   rerolled, rewritten, or re-anchored take never enters canon.
6. Re-anchor after two consecutive output-sourced extensions by default and
   never exceed the schema ceiling of three.

## Stop Conditions

Do not generate when the provider/model is not locked, the contract is
incomplete, an identity ID does not resolve, temporal beats conflict, reference
ownership overlaps, a connected clip has no accepted seven-dimensional source
state, required text is delegated to the model, or prompt review fails.
