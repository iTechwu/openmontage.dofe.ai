# Sequence State

Maintain this compact state for the current scene:

```text
PROJECT ID:
STORY GOAL:
FINAL OUTCOME:
CURRENT SCENE:
CURRENT CLIP:
PARENT CLIP:
CANONICAL REFERENCES:
IDENTITY IDS AND CANONICAL ANCHORS:
ACCEPTED CLIPS:
OBSERVED SUBJECT STATE:
OBSERVED PROP STATE:
OBSERVED ENVIRONMENT STATE:
OBSERVED CAMERA STATE AND SCREEN DIRECTION:
OBSERVED LIGHTING STATE:
OBSERVED AUDIO AND DIALOGUE STATE:
OBSERVED OPEN MOTION:
COMPLETED BEATS:
NEXT CLIP JOB AND FELT INTENT:
NEXT ENDPOINT:
CONTINUITY LOCKS:
ALLOWED CHANGES:
RESERVED FUTURE BEATS:
EXTENSION DEPTH:
UNCERTAINTIES:
```

A still frame can establish static pose, position, appearance, light, and
framing, but not motion vector, camera phase, or audio phase. A full inspected
clip may establish all of them. State which evidence was actually available.

Persist the seven observed dimensions as a closed object. Unknown does not mean
empty: name the missing evidence in `uncertainties`, lower confidence, and say
that the dimension is unverified. Copy this exact boundary into the successor's
`handoff_state`; do not summarize it back into one prose sentence first.
