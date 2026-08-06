# Multi-Shot Grammar

Use explicit shot labels for real cuts inside one generation:

```text
Shot 1 (framing, camera): subject + one action. Sound. End state.
Shot 2 (framing, camera): subject + one action. Sound. End state.
```

Rules:

- Plan about 4-6 seconds for each shot.
- Repeat immutable identity anchors verbatim in each shot block.
- Carry persistent weather, damage, wardrobe, and ambience explicitly.
- Put each spoken line in the shot where its speaker is visible.
- Finish an action before cutting; open the next shot on the new state.
- Do not mix hard-cut shot blocks with a `single continuous take` instruction.
- Keep beats reserved for later generations out of the current prompt.
