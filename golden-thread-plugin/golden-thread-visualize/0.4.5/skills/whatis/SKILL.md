---
name: whatis
description: "Answer what one part of a gt-visualize explainer does, at that part's own altitude (actor or gate: its role; box, store or stack: its role, with a step down to its files; file: the file itself), from the part's note, the scenes it appears in and the project's vault pages. Then offers step into, step out to its scene, step over to the next part in its group, and where for a breadcrumb. A part that mixes concerns is reported as an organisation finding. Use when the user says: what is this part, what does this part do, whatis, what is this box in the explainer, explain this part of the story, where does this part sit in the explainer."
model_intent: balanced
---

# Whatis: one part, at its own altitude

**Under gt sandbox mode** (the session-start line says SANDBOX MODE; `gt_settings.py get sandbox_mode` prints `on`) Claude's shell and file tools cannot write the vault, and by default cannot read it. Every vault step below then takes the route in this table instead: a gt-vault MCP tool runs the same script, with the same checks, outside the sandbox. Read vault files with `vault_read`, `vault_search` and `vault_list`. With sandbox mode off (the default) nothing changes: the shell steps run as written.

| Shell step | Under gt sandbox mode |
|---|---|
| reading `design.md`, `decisions.md`, `source.md` | `vault_read` |
| `gt_visualize.py story …` / `explain …` on `Projects/<slug>/visualize/story.json` | `vault_read` the story, write it to the session's scratch folder, and pass that copy to the script |

Command: `/gt-visualize:whatis <part-id>`. A label instead of an id is fine: find the id in
the `story part` refusal message, which lists every part id.

Script: `<base>/../../scripts/gt_visualize.py` (`<base>` is this skill's base directory).

Find the story as the tour skill does: the one the user names, else
`Projects/<slug>/visualize/story.json` in the vault; none → say so and offer the gt-visualize
skill's explain flow. A story with problems is refused; relay them and stop.

## Answer

1. `python3 <script> story part <story> <part-id>` — the part, its `altitude` by kind, its
   group and neighbours, links in and out, and the scenes it appears in (`focused_in` are the
   scenes about it).
2. **Prediction pause.** Before answering, ask: *"From its name and where it sits, what do you
   think <label> does?"* Stop and wait for the answer (the user may say "skip").
3. **Answer at the part's altitude** — no higher, no lower:
   - `L2` (actor, gate, box, store, stack): its role in one or two sentences, from its `note`,
     the bodies and captions of the scenes in `focused_in`, and its links (who hands it what,
     and what it hands on). For a gate, say what it can refuse, using the story's block flows.
   - `L3` (file): what the file is and who reads or writes it, from the same sources plus the
     project's vault pages (`design.md`, `decisions.md`) where they name it.
   - Say where the answer came from: "from its note", "from scene 3", "from design.md".
   - A part with no note, no focused scene and nothing in the vault: say the story does not
     say what it does. Do not fill the gap from memory or from the code unread.
4. **Organisation finding.** If the part's role needs two unrelated sentences, or its
   `groups_linked` spans most of the groups with unrelated labels, say it looks like it mixes
   concerns and suggest splitting it in the story (via the explain flow). This is a finding
   about the story, not a change you make.
5. **Offer the next move:**
   - `step into` → `/gt-visualize:trace <part-id>` (for box/store/stack, `step_into` names L3);
   - `step out` → its first `focused_in` scene (`story scene <story> <index>`), at L1;
   - `step over` → `neighbours.next` in its group (`whatis` again);
   - `where` → breadcrumb: `<story title> › <group> › <label> (L2|L3)`.

## Rules

- Draw only from the story JSON and the project's vault pages. Never invent a value, a
  behaviour or a file name.
- Read-only: this skill writes nothing.
