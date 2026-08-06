---
name: seedance-provider
description: Select and configure Seedance video-generation providers, operations, model variants, references, duration, aspect ratio, audio, and cost behavior. Use before invoking any OpenMontage Seedance route through DoFe, fal.ai, Replicate, Runway, or Higgsfield.
---

# Seedance Provider

Use this skill for execution facts. Use
[seedance-directing](../seedance-directing/SKILL.md) for shot design,
[seedance-continuity](../seedance-continuity/SKILL.md) for connected clips,
[seedance-prompting](../seedance-prompting/SKILL.md) for prompt compilation, and
[seedance-quality](../seedance-quality/SKILL.md) for prompt/take review.

## Provider Contract

1. Discover the tool through the registry and prefer `video_selector` unless a
   provider was explicitly approved.
2. Announce the exact tool, provider, model/variant, operation, and whether the
   call is a sample or batch before spending.
3. Verify the selected tool's `supports` contract. Do not infer features from
   the Seedance family name or another provider's endpoint.
4. Resolve reference-token syntax from the selected provider surface. Treat
   fal, DoFe, Runway, Higgsfield, and Replicate as separate prompt surfaces;
   never normalize their tokens to a universal `@Image1` form. If the selected
   tool contract does not declare prompt-token syntax, use its input parameter
   and mark the reference `binding_mode=input_parameter`; do not invent a token.
5. Pass an explicit `output_path` under the active project workspace.
6. Preserve the approved provider/model path. Stop for approval before any
   fallback changes that path.

## Execution Preflight

Before every paid call, run `video_selector` with `operation=preflight`, the
real `target_operation`, locked model/variant, intended duration, ratio,
resolution, references, and `reference_roles`. Keep `live_preflight=true`.

- `passed` with `verification_level=live_provider_contract` means the provider
  exposed a side-effect-free model/input contract and the exact payload passed.
- `degraded` means only the current OpenMontage tool contract was verified. A
  representative sample may proceed, but a batch must stop unless the user
  explicitly approves `allow_degraded_preflight=true`.
- `blocked` means credentials, model access, declared fields, operation, or
  reference binding are incompatible. Do not call the provider.

Treat `input_schema_fingerprint`, `provider_contract_version`, and
`reference_binding` as provenance facts. A dependency check alone never proves
model entitlement. Do not claim live verification when `live_probe.status` is
`not_supported` or `unverified`.

## OpenMontage Routes

| Tool | Route | Use |
|---|---|---|
| `dofe_video` | models.dofe.ai gateway | Project-configured Seedance alias, T2V/I2V/reference-to-video |
| `seedance_video` | fal.ai | Standard or fast, native audio, rich references |
| `seedance_replicate` | Replicate | Standard or fast T2V/I2V |
| `runway_video` | Runway | Only apply this skill when a Seedance model is selected |
| `higgsfield_video` | Higgsfield | Only apply this skill when a Seedance model is selected |

Registry contracts outrank this table when they differ.

## Operation Choice

- Use `text_to_video` for exploration, establishing shots, and shots without a
  canonical identity/product anchor.
- Use `image_to_video` when a first frame, vehicle, character, product, costume,
  or composition must remain recognizable.
- Use `reference_to_video` only when the selected tool exposes it. Assign every
  reference a single primary role before compiling the prompt.
- Treat first/last-frame, native extend, edit, video references, and audio
  references as provider-operation capabilities. Never promise them without a
  positive registry contract.

## Parameter Policy

- Use 5-8 seconds for one controlled hero action, 10-15 seconds only when the
  provider supports the required multi-shot density.
- Use the standard/high-quality variant for dialogue, identity-critical shots,
  multi-shot scenes, slow motion, or complex camera movement. Use fast for
  composition tests and simple inserts.
- Lock the seed after composition and identity read correctly; retain it in the
  asset manifest for controlled retakes.
- Set `generate_audio=false` when OpenMontage supplies approved TTS, music, and
  sound design separately. Set it true only when native synchronized audio is
  part of the approved plan.
- Do not ask the model to render required text, plates, subtitles, or logos.
  Generate the clean visual and guarantee text in compose/post.

## Failure Boundary

A provider error is not a prompt-quality error. Report auth, access, model,
quota, endpoint, and timeout failures separately from visual drift or weak
direction. Do not rewrite a good prompt to compensate for an unavailable API.

This skill incorporates MIT-licensed workflow knowledge from
[`Emily2040/seedance-2.0`](https://github.com/Emily2040/seedance-2.0); see
[LICENSE](LICENSE).
