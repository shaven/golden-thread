---
name: tour
description: "Walk a newcomer through a gt-visualize explainer scene by scene: scene 1 is the system's job in one scenario, and before each next scene a prediction pause asks what connects its parts, then the scene is the reveal and its flows are the hop map. At any scene the user can step into a part or step over to the next scene. Progress is saved per project so a later session resumes with a two-question recap. Drawn only from the story JSON and the project's vault pages. Use when the user says: tour the explainer, give me a tour of the explainer, walk me through the explainer, step through the explainer scenes, resume the explainer tour, continue the explainer tour."
model_intent: balanced
---

# Tour: a guided walk through an explainer

Command: `/gt-visualize:tour [focus]`. `focus` is optional: a scene title or index to start
at, or a part id (start at the first scene that focuses it).

Script: `<base>/../../scripts/gt_visualize.py` (`<base>` is this skill's base directory).

## Altitudes

The walk moves between altitudes, and says which one it is at:

| Level | What it answers |
|---|---|
| L0 | the system's job, in one scenario (scene 1) |
| L1 | the hop map: which part hands what to which (a scene's flows) |
| L2 | one part's role |
| L3 | a function or a file |
| L4 | real values, from a real run (only through `/gt-visualize:trace` with a real test) |

## Find the story

1. The story the user names; else `Projects/<slug>/visualize/story.json` in the vault for the
   project this session is working on (ask in one line if the project is unclear).
2. No story: say so and offer the gt-visualize skill's **explain** flow to write one. Do not
   tour from memory, and do not write a story as a side effect of touring.
3. Every command below validates the story first; one with problems is refused with the list.
   Relay the problems and stop — a broken story is fixed, never toured around.

## Resume

`python3 <script> tour-state get --project <slug> --vault <vault>`

- `saved: false` → a fresh tour from scene 1 (or from `focus`).
- Saved → ask whether to resume at the saved scene. Before resuming, give a **two-question
  recap**: `python3 <script> story questions <story> --parts <ids>` scoped to the parts of the
  scenes in `seen`; ask two of them, one at a time, with the same prediction pause as below.
  The expected answers are the `expected` strings; never grade against anything else.
- `story_changed: true` → say the story changed since the last session, and offer to start
  over rather than resume.

The file's `_header` says it is private and not a metric. Treat it that way: never report it,
score it, or compare it with anyone else's.

## The walk

For each scene, `python3 <script> story scene <story> <index>` gives the scene as JSON.

1. **Scene 1 (L0).** Read out the scene's body and caption: the system's job in one scenario.
   Name the groups; every later scene sits on this map.
2. **Prediction pause — before every later scene.** Name the scene's `shown` parts (labels,
   not ids) and ask: *"What do you think connects these parts?"* Then **stop and wait** for the
   answer. Do not reveal in the same message. If the user says "skip" or "just show me", reveal.
3. **Reveal.** The scene body, then the caption, then the hop map (L1) as a short numbered
   list: `from → to (kind): label`. Say where the user's prediction matched a hop and where it
   did not — using only the hops, never an invented connection.
4. **Offer the next move:**
   - `step into <part>` → `/gt-visualize:trace <part>` (or, for a quick role answer,
     `/gt-visualize:whatis <part>`);
   - `step over` → the next scene (`next` in the JSON);
   - `where` → "scene i of n: <title>", and the altitude;
   - `stop` → save and end.
5. **Save after each scene:**
   `python3 <script> tour-state set --project <slug> --story <story> --scene <index> --vault <vault>`
   (add `--stepped-into <part>` when the user stepped into one; `--finished` after the last
   scene). Exit 5 means another live session has claimed the file: say so, keep touring, and
   do not save — never work around a claim.
6. **The last scene** is the recap: read it, and name the hops the user has now seen.

## Rules

- Everything said comes from the story JSON (body, caption, labels, notes, hops) and the
  project's vault pages. No value, number or behaviour that is not in them. When the user asks
  something the story does not answer, say so and offer `/gt-visualize:trace`.
- The prediction pause is never skipped by you; only the user can skip it.
- Progress is written only to `Projects/<slug>/visualize/tour-state.json`, only through
  `tour-state set`. It is private and not a metric.
- This skill reads the story; it never edits it. A part that the story gets wrong is reported
  as a finding for the explain flow to fix.
