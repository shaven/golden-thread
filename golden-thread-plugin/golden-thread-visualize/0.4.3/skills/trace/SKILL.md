---
name: trace
description: "Run one scene (or the scene that focuses one part) of a gt-visualize explainer forward, one hop at a time, with a prediction pause before each reveal, grounded in a real test of that path when one exists; with no runnable path it is a clearly labelled STATIC walkthrough of types and control flow with no invented values. Ends with a hop table and offers to add a scenario test, writing it only on the user's yes. Use when the user says: trace this scene, trace this part, trace the scene forward, follow the hops, trace the request path through the explainer, run this scene forward hop by hop."
---

# Trace: a scene, one hop at a time

Command: `/gt-visualize:trace <scene-or-part>` — a scene title or 1-based index, or a part id.

Script: `<base>/../../scripts/gt_visualize.py` (`<base>` is this skill's base directory).

Find the story as the tour skill does (named by the user, else
`Projects/<slug>/visualize/story.json` in the vault). A story with problems is refused; relay
them and stop.

## Pick the scene

- A scene: `python3 <script> story scene <story> <title|index>`.
- A part: `python3 <script> story part <story> <id>`, then the first scene in `focused_in`
  (none → the first scene in `scenes` other than 1; say which you chose).
- A scene with no `hops` has nothing to trace: say so, and offer `/gt-visualize:whatis` on
  its focus.

## Ground it — live or STATIC

1. Look in the code for a test that exercises this path: a test naming the scene's parts, its
   entry point or the files the vault's `design.md` ties to them. Read it before claiming it
   covers the path.
2. **LIVE** — such a test exists: run it (the project's own test command, narrowed to that
   test). Use only what that run shows — its inputs, outputs, logs — as the L4 values at each
   hop. A test that fails is reported as it is; the trace stops at the failing hop.
3. **STATIC** — no test, or none that runs here: say, before the first hop,
   **"STATIC walkthrough — no test ran; types and control flow only, no values."** Every hop
   is then described by the types and calls the code shows. Never write an example value, a
   sample payload or a number that did not come from a run.

## The hops

For each hop `n` in `hops`, in order:

1. **Prediction pause.** *"Hop n: <from label> → <to label>. What do you expect crosses, and
   why?"* Stop and wait for the answer (the user may say "skip").
2. **Reveal:** where in the code the hop happens (file and function, read — not guessed), what
   goes in and what comes out (LIVE: the run's values; STATIC: types), and why, from the scene
   body, the link label or the vault. A `block` hop: what is refused and what comes back. A
   `return` hop travels back along its link.
3. Say where the user's prediction matched.

## Close

A hop table, one row per hop:

| hop | location | in → out | why |
|---|---|---|---|

Label the table **LIVE (<test name>)** or **STATIC**.

Then **offer** — do not do — to add a scenario test for this path when the trace was STATIC,
or when the test only covers part of it. Describe the test in two lines (what it drives, what
it asserts) and ask. **Write it only on the user's yes**, in the project's own test layout,
and run it once written.

## Rules

- No invented values, ever. LIVE values come from a run; STATIC has none and says so.
- Nothing is written without the user's yes to the offered test. The story is never edited.
