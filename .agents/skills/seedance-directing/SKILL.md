---
name: seedance-directing
description: Design controllable Seedance story beats, shot contracts, multi-shot grammar, blocking, camera, lighting, performance, and sound. Use during script and scene planning for narrative, commercial, character, action, dialogue, or cinematic video.
---

# Seedance Directing

Turn story intent into a shot that can be seen and heard. Do not use
`cinematic`, `epic`, or an emotion adjective as a substitute for direction.

## Directing Workflow

1. Classify the beat as `narrative` or `utility`.
2. For narrative work, complete the Director's Read in
   [references/shot-contract.md](references/shot-contract.md). For utility work,
   state the concrete demonstration goal and the drama that must not be added.
3. Choose one felt intent: what the viewer should notice or feel after this
   shot that was not true before it.
4. Give the shot one primary spend: identity, motion, or scene density. Split
   the shot if all three are required at maximum fidelity.
5. Make framing, lens, camera movement, blocking, light, performance, sound,
   and endpoint serve the same intent.
6. Translate every abstract idea into a filmable carrier. A hidden conflict may
   become a withheld gesture, interrupted task, eyeline, spatial retreat, or
   contradiction between dialogue and body.
7. Persist the result in `scene_plan.scenes[].generation_contract`, including
   `provider_family="seedance"` and the structured `seedance_contract`.

## Shot Contract

Every generated scene must define:

- felt intent and narrative/utility lane;
- subject identity anchors and immutable attributes;
- planned start and completed endpoint;
- one primary action per shot;
- shot size, subject position, depth, lens feel, and one motivated camera move;
- one physically motivated light source and persistent environment;
- observable performance or material behavior;
- dialogue, ambience, SFX, and music intent;
- allowed changes and explicit exclusions;
- primary prompt spend and intentionally economized detail.

## Multi-Shot Rule

Read [references/multishot.md](references/multishot.md) when a single generation
contains cuts. Use explicit `Shot 1`, `Shot 2`, and `Shot 3` blocks. Budget
roughly 4-6 seconds per shot, give each block one action and one camera move,
and end each block on a completed visual state. Use a single continuous-take
contract when continuity of motion matters more than coverage.

## Dialogue And Characters

- Give each speaker a stable screen position, eyeline, physical objective, and
  short line. Use reaction shots and pauses so dialogue reads as an exchange.
- Keep one focal performance beat per short clip. Background characters retain
  simple, persistent behavior.
- Repeat the exact identity anchor in every cold-start shot. Do not rely on
  pronouns or `the same character` alone.
- For anthropomorphic vehicles, lock body class, silhouette, paint, face
  placement, lights, wheels, damage state, and any post-production plate zone.

## Output

Return an internal shot contract and a concise natural-language description for
the scene plan. Do not compile the provider prompt here; use
`seedance-prompting` after continuity has been resolved.

Derived from MIT-licensed
[`Emily2040/seedance-2.0`](https://github.com/Emily2040/seedance-2.0); license is
retained at `../seedance-provider/LICENSE`.
