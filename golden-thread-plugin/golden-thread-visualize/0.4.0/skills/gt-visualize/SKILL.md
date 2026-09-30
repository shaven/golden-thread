---
name: gt-visualize
description: "Show a codebase in 3D, two ways. EXPLAIN: a scroll-driven 3D walkthrough of how a system's parts work together — scenes that highlight parts and animate the flows between them, written from the code and the vault. CODE CITY: every directory a district, every file a building, height by lines, colour by language or git churn. Both render one self-contained offline HTML file with three.js inlined, which can then be PUBLISHED to a claude.ai Artifact, GitHub Pages or a gist after a scrub gate and the user's yes. Use when the user says: explain how this codebase works, show how the parts work together, architecture walkthrough, visualize how it works, visualize this codebase, visualize the repo, show me the code in 3D, code city, 3D view of the repo, map this codebase, which files change the most, publish the visualization, share the walkthrough."
---

# Golden Thread Visualize

Two read-only views of a codebase. Neither writes the code or the vault; each writes one
HTML file. (For how *knowledge* moved through the vault, that is `gt-flow`, not this.)

Script: `<base>/../../scripts/gt_visualize.py` (`<base>` is this skill's base directory).

**Which one?** "How does it work / how do the parts fit / walk me through it" → **explain**.
"What does the repo look like / where is the code / what changes most" → **code city**.
When unsure, ask in one line.

## Explain: how the parts work together

The page is a story: a narrative column that scrolls beside a 3D stage. Each scene shows
some parts, puts one or two in focus, and animates flows along the links between them. You
write the story; the script renders it.

1. **Learn the system before drawing it.** Read, in this order and only as far as needed:
   the project's vault `source.md` (where the code lives), `design.md`, `decisions.md`,
   then the repo's README and entry points (main scripts, installers, services, config,
   hooks). The story must be true: every part must exist and every flow must be something
   the code actually does. When a claim rests on a guess, leave it out.
2. **Pick 8–30 parts** — the things a newcomer must know exist: services, stores, queues,
   config files, external actors, gates that can refuse something. Not every file.
   Group them into 3–6 neighbourhoods (`group`) that read left to right in the order
   work moves through them (the page lays groups out as columns in first-appearance order).
3. **Write 4–10 scenes in this shape — it is required, not a suggestion:**
   - **Open wide.** Scene 1 is the establishing shot: `"show": "*"`, no `focus`, no `flows`.
     Its job is to name the neighbourhoods so every later scene has a map to sit on.
   - **One mechanism per middle scene.** Each answers one question ("What happens on
     install?"). Show the 3 or more parts involved, `focus` the 1–2 doing the work, and put
     `flows` on the links it uses — never more than 5.
   - **Close wide.** The last scene is the recap: `"show": "*"`, flows optional — the reader
     leaves with the whole picture, not a close-up.
   - Every scene has a one-line **`caption`** saying what the picture shows ("The prompt
     passes the gate while the rules travel down from the vault"). The body says *why*; the
     caption says *what to look at*.
4. Write the story JSON to a temp file **outside the vault**, then check it:
   `python3 <script> explain <story.json> --check` — fix every problem it lists and re-run
   until it prints `ok`. Each problem names the rule it breaks (below); fix the story, never
   argue with the rule.
5. Render: `python3 <script> explain <story.json> [--out <file-or-dir>]`. Open the file it
   prints (`open` / `xdg-open` / `start ""`). Tell the user: scroll to step through, drag to
   look around, hover a part for its note, the dots on the right jump to a scene.
6. Offer to keep the story: it is the reusable part. If the user wants it, save it next to
   the project (for example `Projects/<slug>/visualize/story.json` in the vault, under the
   usual claim-before-write rules) so the next render starts from it.
7. Offer the guided walkthrough, which reads a saved story and nothing else:
   - `/gt-visualize:tour [focus]` — scene by scene, with a prediction pause before each
     reveal; progress is saved per project so a later session can resume.
   - `/gt-visualize:whatis <part-id>` — one part at its own altitude, with step into / out /
     over.
   - `/gt-visualize:trace <scene-or-part>` — a scene run forward hop by hop, on a real test
     where one exists, else a labelled STATIC walkthrough.
   - `/gt-visualize:explain-back [--diff <ref>]` — opt-in only: the user explains the seams
     back. Never run it unasked.

### The rules

These exist because a story left to taste came out as a string of close-ups — one or two
parts filling the screen from the first scene on, with no map to place them in. A story
that follows them comes out readable. **Do not work around them.**

**Story rules — `--check` enforces these; a story that breaks any is never rendered:**

| Rule | Requirement |
|---|---|
| S1 | Scene 1 is the establishing shot: `"show": "*"`, no focus, no flows |
| S2 | The last scene is the recap: `"show": "*"` |
| S3 | 4–10 scenes |
| S4 | 8–30 parts, in 3–6 groups |
| S5 | A part label is at most 24 characters — put detail in `note` |
| S6 | A middle scene focuses 1–2 parts, each of them shown |
| S7 | A middle scene shows at least 3 parts — never a lone close-up |
| S8 | At most 5 flows a scene, each on a link whose two ends are shown |
| S9 | A scene body is 1–2 paragraphs of at most 600 characters |
| S10 | Every scene has a `caption` (at most 160 characters) saying what the picture shows |

**Framing rules — the page enforces these itself; no story can change them:**

| Rule | What the camera does |
|---|---|
| F1 | Scene 1 opens pulled back on the whole system |
| F2 | It frames everything a scene *shows*, not just the focus — focus is shown by light, not by zooming in |
| F3 | It never comes closer than 55% of the establishing shot, so every scene keeps its surroundings |
| F4 | One viewing angle for the whole page; only the target and the distance move |
| F5 | Parts outside a scene stay as faint ghosts (turn off per scene with `"ghost": false` only when the extra parts would genuinely mislead) |
| F6 | The last scene pulls back out to the whole system |

### The story format

```json
{
  "title": "How <system> works",
  "subtitle": "one sentence",
  "intro": ["a paragraph or two"],
  "source": "what this was drawn from, and when",
  "parts": [
    {"id": "api", "label": "API server", "kind": "box", "group": "Services",
     "size": "m", "note": "what it does, one sentence", "at": [x, z]}
  ],
  "links": [
    {"from": "user", "to": "api", "label": "request"},
    {"id": "deny", "from": "gate", "to": "user", "label": "refused"}
  ],
  "scenes": [
    {"title": "The parts", "body": ["…"], "caption": "Every part, from above.", "show": "*"},
    {"title": "…", "body": ["…", "…"], "caption": "what the picture shows",
     "show": ["user", "api", "gate"], "focus": ["gate"],
     "flows": [{"link": "user>api", "kind": "data"}]},
    {"title": "All together", "body": ["…"], "caption": "Back to the whole picture.",
     "show": "*"}
  ]
}
```

- `kind`: `box` (a component), `store` (database, folder, repo), `actor` (a person or a
  session), `stack` (layers, tiers, a cache), `gate` (something that checks or refuses),
  `file` (one config or data file). `size`: `s`, `m`, `l`. `at` is optional — leave it out
  and the layout is automatic.
- A link's id is `"from>to"` unless you give one; give one when two links join the same pair.
- Flow `kind`: `data` (calls, requests, files), `rule` (config or policy pushed in),
  `return` (an answer travelling back — drawn from `to` to `from`), `block` (stopped at a gate).
- Text may use `code` and **bold**; everything else is escaped. At most 160 links; the
  other limits are the rules above.
- **Names are real.** An explainer is for people who may see the system; if the page will
  leave the user's machine, ask whether any host, customer or employer name must come out,
  and rewrite those parts' labels generically before rendering.

## Code city: where the code is

1. Decide what to draw: the path the user named, else the current git work tree, else the
   current directory. Say which.
2. Decide who will see the page.
   - **Only the user, on this machine:** real names are fine.
   - **Anyone else, or anywhere else** — an Artifact, a chat, a ticket, email, a slide,
     a screenshot, a file copied off this machine: **always add `--redact`**. No
     exception, even when the user says the names are not sensitive — render a second,
     redacted file for sharing instead. Never publish or attach an unredacted render.
3. Run:
   `python3 <script> render <path> [--redact] [--since DAYS] [--no-churn] [--exclude GLOB ...] [--max-files N] [--out <file-or-dir>]`
   - `--since` sets the churn window (default 90 days). Churn needs git; outside a
     repository it is off and the output line says so.
   - `--exclude` is repeatable and matches the repo-relative path or the basename.
   - Above `--max-files` (default 20,000) the deepest directories collapse into single
     buildings; the output line says at which depth.
4. Open the file it prints. Tell the user: plots are directories, buildings are files;
   height is lines, footprint is size, colour is language (legend bottom-left). Drag to
   orbit, scroll to zoom, hover for details, click a district to fly to it, **Churn**
   recolours by recent changes, the search box highlights matching paths.

Redacted pages replace every file and directory name with a short salted hash
(`d-3fa2c1` for a directory, `f-91c0aa` plus the file's extension when it is short and
plain), keeping the shape of the tree. If the script reports that its redaction self-check
failed, nothing was written: report it, never work around it.

## Publish: sharing a page

Only when the user asks to share or publish a page. A render is local until then.

1. **Check first:** `python3 <script> publish <page.html> --check [--target T] [--visibility V]`.
   The target defaults to the `visualize_publish` setting (`local` until the user chooses) and
   the visibility to `visualize_publish_visibility` (`private`). It runs the **scrub gate** —
   credential scan, IPv4 addresses, home-folder paths, and the user's scrub terms — and
   refuses on any hit, naming the kind of problem and never the value. Fix the SOURCE (the
   story's labels and notes, or `--redact` for a code city), re-render, re-check. Never edit
   the HTML to slip past the gate.
2. **Show the user the plan it prints** — target, visibility, final URL — and ask. One yes per
   document; do not ask twice for `public`.
3. On yes: the same command with `--yes`.
   - `local`, `github-pages`, `gist`: the script publishes and records it.
   - **`claude`**: the script cannot reach claude.ai. It prints `ready for Claude`; publish the
     file with the **Artifact tool** (update the URL it names, if any, so the link stays the
     same), then record it: `publish <page.html> --target claude --url <artifact url> --yes`.
4. Tell the user the URL. `gt_visualize.py publishes` lists everything published, so any page
   can be found and taken down later.

Targets and what they can deliver — a visibility a target cannot enforce is refused:

| Target | Visibility | Needs |
|---|---|---|
| `local` | any (it stays here) | nothing |
| `claude` | `private`, `link` (shared from claude.ai) | the user's Claude login |
| `github-pages` | `public` only | `targets set github-pages repo=<clone> base_url=<https://…> [dir=visualize]`; the user's git login |
| `gist` | `link` (secret gist), `public` | `gh` logged in. GitHub shows a gist's HTML as **source**, not as a page — say so every time |

**Never store a credential.** Every target uses a login the user already has; `targets set`
refuses anything that looks like a password or token. Set scrub terms with
`targets set scrub_terms=<file>` (one term per line — employer, customer or host names).

## Output and exits

Without `--out` the file lands in the current directory, or in a temp directory when the
current directory is inside the vault; an `--out` inside the vault is refused. The page
works offline — three.js is inside the file, pinned by hash; a bundle that fails its hash
is refused, never inlined.

- 1 — a bad argument, an unreadable path or story, a story with problems (each one is
  listed), or a refused `--out`; relay the message.
- 3 — code city only: nothing to draw; show the command and the excludes.
- 4 — publish only: the plan was printed and nothing was published, because `--yes` was not
  given. Show the user the plan.

## Rules

- Read-only: never write the tree being drawn, and never write the page inside the vault.
- A story states only what the code and the vault show. No invented parts or flows.
- `--redact` before a code city leaves the user's own screen, every time.
- Nothing is published without the scrub gate passing and the user's yes to the printed plan.
