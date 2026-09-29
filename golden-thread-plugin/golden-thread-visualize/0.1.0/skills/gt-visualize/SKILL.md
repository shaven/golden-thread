---
name: gt-visualize
description: "Show a codebase in 3D, two ways. EXPLAIN: a scroll-driven 3D walkthrough of how a system's parts work together — scenes that highlight parts and animate the flows between them, written from the code and the vault. CODE CITY: every directory a district, every file a building, height by lines, colour by language or git churn. Both render one self-contained offline HTML file with three.js inlined. Use when the user says: explain how this codebase works, show how the parts work together, architecture walkthrough, visualize how it works, visualize this codebase, visualize the repo, show me the code in 3D, code city, 3D view of the repo, map this codebase, which files change the most."
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
2. **Pick 8–25 parts** — the things a newcomer must know exist: services, stores, queues,
   config files, external actors, gates that can refuse something. Not every file.
   Group them into 3–6 neighbourhoods (`group`) that read left to right in the order
   work moves through them (the page lays groups out as columns in first-appearance order).
3. **Write 5–10 scenes** that each answer one question ("What happens on install?",
   "What happens to a request?"). First scene: `"show": "*"` — the whole picture. Then one
   mechanism per scene, `"ghost": true`, `focus` on the part doing the work, and `flows`
   along the links it uses. Body: two short paragraphs, plain language, `code` for names.
4. Write the story JSON to a temp file **outside the vault**, then check it:
   `python3 <script> explain <story.json> --check` — fix every problem it lists and re-run
   until it prints `ok`.
5. Render: `python3 <script> explain <story.json> [--out <file-or-dir>]`. Open the file it
   prints (`open` / `xdg-open` / `start ""`). Tell the user: scroll to step through, drag to
   look around, hover a part for its note, the dots on the right jump to a scene.
6. Offer to keep the story: it is the reusable part. If the user wants it, save it next to
   the project (for example `Projects/<slug>/visualize/story.json` in the vault, under the
   usual claim-before-write rules) so the next render starts from it.

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
    {"title": "…", "body": ["…", "…"], "show": "*" | ["id", …], "focus": ["id"],
     "flows": [{"link": "user>api", "kind": "data"}], "ghost": true}
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
- Text may use `code` and **bold**; everything else is escaped. Limits: 80 parts, 160 links,
  24 scenes.
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

## Output and exits

Without `--out` the file lands in the current directory, or in a temp directory when the
current directory is inside the vault; an `--out` inside the vault is refused. The page
works offline — three.js is inside the file, pinned by hash; a bundle that fails its hash
is refused, never inlined.

- 1 — a bad argument, an unreadable path or story, a story with problems (each one is
  listed), or a refused `--out`; relay the message.
- 3 — code city only: nothing to draw; show the command and the excludes.

## Rules

- Read-only: never write the tree being drawn, and never write the page inside the vault.
- A story states only what the code and the vault show. No invented parts or flows.
- `--redact` before a code city leaves the user's own screen, every time.
