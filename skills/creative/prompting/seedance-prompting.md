# Seedance Prompting Compatibility Route

This path is retained for existing OpenMontage references. The canonical Layer
2 workflow is `skills/creative/seedance-production.md`; follow it for provider
selection, scene contracts, prompt compilation, generation, take review, and
continuity handoff.

## Required Sources

Read current execution facts from the selected tool contract and registry, then
load the input-aware `required_agent_skills` returned by `video_selector`.
Seedance routes currently use these functional Layer 3 skills:

- `.agents/skills/seedance-provider/SKILL.md`
- `.agents/skills/seedance-directing/SKILL.md`
- `.agents/skills/seedance-continuity/SKILL.md`
- `.agents/skills/seedance-prompting/SKILL.md`
- `.agents/skills/seedance-quality/SKILL.md`

Do not keep provider availability, model support, cost, duration, resolution,
reference limits, or endpoint behavior in this file. Those facts change and
belong to the selected tool's live registry contract.

## Prompt Boundary

Compile from `scene_plan.scenes[].generation_contract`, not from a free-form
scene description. Keep exact surface-specific reference tokens, one primary
action and motivated camera move per short shot, concrete light/performance/
sound carriers, continuity locks, completed/reserved beat exclusions, and a
completed endpoint. Required text, logos, plates, and subtitles stay in post.

For a connected sequence, only an accepted observed state may open the next
prompt. Store the preflight in `prompt_review`, inspect the generated media, and
store the canon decision in `take_review` before compiling another clip.
