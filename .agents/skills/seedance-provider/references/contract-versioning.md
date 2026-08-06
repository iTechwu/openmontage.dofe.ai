# Seedance Contract Versioning

Treat a missing `seedance_contract_version` as legacy `1.0`. Historical v1
scene plans, assets, and lineage reviews remain readable and checkpoint-valid.
Do not rewrite archived checkpoints only to add fields.

Before editing a legacy Seedance scene or spending on another generation:

1. Preserve the v1 artifact as history and upgrade the active artifact only.
2. Set `seedance_contract_version="2.0"` on each active Seedance generation
   contract and its resulting asset. Set the same version on `lineage_review`.
3. Resolve recurring subjects into `scene_plan.identity_registry` and replace
   scene-local identity guesses with `identity_ids`.
4. Inspect source material and fill ordered temporal beats, full shot geometry,
   provenance for authored specifics, and the seven-dimensional handoff state.
   Mark uncertainties explicitly; never manufacture an observation.
5. Recompile the prompt into `prompt_review.compile_spec`, record identity and
   returned-state observations, then rerun the scene and lineage reviews.
6. Checkpoint only after every active Seedance record is v2 and all reviewer
   findings are resolved or explicitly accepted.

This is an agent-led migration because identity, provenance, temporal intent,
and observed media state require evidence and creative judgment. Python and
JSON Schema validate the completed record but must not invent those values.
