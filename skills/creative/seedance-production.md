# Seedance Production Route

Use this Layer 2 route whenever `video_selector` ranks or selects a tool whose
`required_agent_skills` includes `seedance-provider`. It applies to every
pipeline, not only cinematic.

## Before Scene Planning

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

For every selected Seedance scene, set
`generation_contract.model_family="seedance"` and complete the schema's
`seedance_contract`:

- choose the `narrative` or `utility` lane;
- persist the complete lane-specific authoring state;
- specify one primary action, framing, camera, motivated light, observable
  behavior, and sound intent;
- compile internal story choices into concrete `prompt_carriers`;
- record exclusions, accepted source status, observed opening state when
  available, extension depth, confidence, and uncertainties.

Reference tokens are surface-specific. Set `binding_mode=prompt_token` only
when the selected tool contract declares the syntax and record that exact token
in `reference_roles[].tag`. Otherwise use `input_parameter` and a stable internal
tag; never assume `@Image1` is portable across providers.

## Asset Generation

1. Compile only the current scene from `generation_contract`; future scene
   prompts remain provisional.
2. Run `seedance-quality` preflight and store the draft, critique, final prompt,
   and skills in `prompt_review`.
3. Require provider preflight to be non-blocking. Generate one representative
   sample before a batch. A degraded live probe may support the sample, but the
   batch requires explicit approval recorded as `allow_degraded_preflight=true`.
4. Inspect the actual media, set `model_family="seedance"`, and write a
   schema-valid `take_review`.
5. Only accepted observed state may seed the next connected clip. A rejected,
   rerolled, rewritten, or re-anchored take never enters canon.
6. Re-anchor after two consecutive output-sourced extensions by default and
   never exceed the schema ceiling of three.

## Stop Conditions

Do not generate when the provider/model is not locked, the contract is
incomplete, reference ownership overlaps, a connected clip has no accepted
source state, required text is delegated to the model, or prompt review fails.
