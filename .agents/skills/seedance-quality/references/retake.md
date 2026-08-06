# Retake Protocol

1. Quote the failing prompt clause or name the missing contract field.
2. Classify the root cause: provider, input/reference, prompt, generation, or
   post-production.
3. Decide whether the defect is cheaper and safer to fix in post.
4. If regenerating, change one primary variable only: duration, shot density,
   camera, identity anchoring, action wording, mode, or reference set.
5. Keep the approved provider/model/variant unless the user approves a change.
6. Limit blind rerolls. After two similar failures, rewrite or split the shot.
7. For continuation drift, re-anchor from canonical identity references and the
   strongest accepted state rather than extending a degraded output.

Prefer conservative repairs:

```text
[Exact reference role]. Preserve [immutable identity] exactly. One visible
action: [verb and consequence]. Camera: [one move]. Light: [physical source].
Sound: [dialogue/ambience/SFX]. Do not change [locks]. Stop at [endpoint].
```
