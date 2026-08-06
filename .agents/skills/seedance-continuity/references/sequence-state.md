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
IDENTITY ANCHORS:
ACCEPTED CLIPS:
OBSERVED END STATE:
OPEN SUBJECT MOTION:
CAMERA PHASE AND SCREEN DIRECTION:
LIGHT AND ENVIRONMENT STATE:
PROP OWNERSHIP AND DAMAGE:
AUDIO AND DIALOGUE PHASE:
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
