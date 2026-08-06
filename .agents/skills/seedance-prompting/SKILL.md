---
name: seedance-prompting
description: Compile OpenMontage shot and continuity contracts into concise, provider-ready Seedance prompts in Chinese or English. Use for T2V, I2V, reference-to-video, multi-shot, dialogue, action, product, character, and continuation prompts before a Seedance generation call.
---

# Seedance Prompting

Read [seedance-provider](../seedance-provider/SKILL.md) and
[seedance-directing](../seedance-directing/SKILL.md) first. Also read
[seedance-continuity](../seedance-continuity/SKILL.md) for connected clips and
[seedance-quality](../seedance-quality/SKILL.md) before a paid call or after a
failed take.

## Compile Order

1. Reference roles and exact tags.
2. Opening state that attached media cannot carry.
3. Current clip action and observable performance.
4. Framing, lens feel, camera behavior, and screen direction.
5. Physically motivated light, persistent environment, and style constraints.
6. Dialogue, ambience, SFX, and approved music intent.
7. Identity and continuity locks plus allowed changes.
8. Completed/reserved beat exclusions.
9. The precise visual endpoint: `Stop when...`.

Use only the current clip. Do not include the full story or future prompts.

## Prompt Shape

```text
[Shot structure]. [Reference roles]. [Subject identity]. [Opening state].
[Action in temporal order]. [Camera and framing]. [Light/environment/style].
[Dialogue/sound]. [Locks and exclusions]. Stop when [completed endpoint].
```

Use `Shot N:` blocks for actual cuts and temporal phases for a continuous take.
Do not mix both structures.

## Precision Rules

- Prefer concrete verbs, positions, material behavior, and consequences over
  mood adjectives.
- Give each shot one primary action and one camera move.
- Repeat immutable identity anchors verbatim across independent shot blocks.
- Keep dialogue short, assign every line to a visible speaker, and specify the
  reaction/pause that makes the exchange readable.
- Preserve the selected surface's exact reference tags and explicitly block
  unwanted transfer. Emit a tag only for `binding_mode=prompt_token`. For
  `input_parameter`, describe the reference role without inventing a provider
  token. Never assume `@Image1` syntax works on another route.
- If a source clip is attached, let it carry visible state; text describes the
  delta, open motion, endpoint, and known drift risks.
- Move plates, logos, subtitles, HUDs, and required text to post.

## Chinese Prompts

Write compact native Chinese rather than translating English word-for-word.
Use this order: `镜头结构 -> 参考职责 -> 主体锁定 -> 动作节拍 -> 镜头 -> 光线与环境 -> 声音 -> 约束 -> 结束状态`.
Keep provider reference tags unchanged even when surrounding prose is Chinese.

Example shape:

```text
单一连续镜头。@Image1仅控制车辆身份，严格保持车身轮廓、漆色、灯组和轮毂；
忽略参考图的背景与文字。车辆从湿滑隧道左侧加速切入，右前轮压过积水后车身短暂
侧倾，随即回正。低机位35mm侧向跟拍，不变焦。顶灯逐段扫过车顶，保留冷白反射。
声音只有轮胎水声、引擎负载和远处警报。不要出现可读车牌文字。结束在车辆回正、
车头指向出口的中近景。
```

## Compression Priority

Keep, in order: exact tags/roles, opening delta, action/endpoint, identity locks,
felt-intent carriers, exclusions, camera/motion phase, audio phase. Delete
generic style boosters, repeated reference descriptions, and future-story
summary first.

Derived from MIT-licensed
[`Emily2040/seedance-2.0`](https://github.com/Emily2040/seedance-2.0); license is
retained at `../seedance-provider/LICENSE`.
