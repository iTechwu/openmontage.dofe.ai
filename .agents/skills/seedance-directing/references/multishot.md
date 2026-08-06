# Multi-Shot Grammar

Use explicit shot labels for real cuts inside one generation:

```text
Shot 1 (framing, camera): subject + one action. Sound. End state.
Shot 2 (framing, camera): subject + one action. Sound. End state.
```

Rules:

- Plan about 4-6 seconds for each shot.
- Map each `Shot N` block to one ordered `temporal_beats` record. Do not add a
  cut that has no distinct action or completed state.
- Repeat immutable identity anchors verbatim in each shot block.
- Carry persistent weather, damage, wardrobe, and ambience explicitly.
- Put each spoken line in the shot where its speaker is visible.
- Finish an action before cutting; open the next shot on the new state.
- Do not mix hard-cut shot blocks with a `single continuous take` instruction.
- For a continuous take, use temporal phases instead of `Shot N` labels and
  keep camera axis, screen direction, and open motion explicit at phase edges.
- Keep beats reserved for later generations out of the current prompt.
