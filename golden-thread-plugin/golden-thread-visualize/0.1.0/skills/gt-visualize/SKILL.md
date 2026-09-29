---
name: gt-visualize
description: "Draw a repository as an interactive 3D code city: every directory a district, every file a building — height by lines, footprint by size, colour by language or by how often it changed (git churn). Renders one self-contained offline HTML file with three.js inlined. Use when the user says: visualize this codebase, visualize the repo, show me the code in 3D, code city, 3D view of the repo, map this codebase, what does this repo look like, where is the code in this repo, which files change the most."
---

# Golden Thread Visualize

A read-only view of a source tree. It never writes the tree or the vault; it writes one
HTML file. (For how *knowledge* moved through the vault, that is `gt-flow`, not this.)

Script: `<base>/../../scripts/gt_visualize.py` (`<base>` is this skill's base directory).

## Render

1. Decide what to draw: the path the user named, else the current git work tree
   (`git rev-parse --show-toplevel`), else the current directory. Say which.
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
   - `--exclude` is repeatable and matches the repo-relative path or the basename, for
     vendored or generated trees the user does not want drawn (`--exclude 'vendor/*'`).
   - A tree above `--max-files` (default 20,000) has its deepest directories collapsed
     into single buildings; the output line says at which depth.
   - Without `--out` the file lands in the current directory, or in a temp directory when
     the current directory is inside the vault. An `--out` inside the vault is refused.
4. It prints `wrote <path> (N files, M directories, L lines; language from …; …)`. Open
   that path: `open <path>` on macOS, `xdg-open <path>` on Linux, `start "" <path>` on
   Windows. It works offline — three.js is inside the file; no CDN, no network.
5. Tell the user what they are looking at: plots are directories (nested — a district
   sits on its parent's), buildings are files; height is lines, footprint is size, colour
   is language (legend bottom-left). Drag to orbit, scroll to zoom, right-drag to pan.
   Hover for a file's path, language, lines, bytes and changes; click a district to fly
   to it; **Churn** recolours by changes in the window, cold to hot; the search box
   highlights matching paths; **Reset view** returns to the whole city.

Redacted pages replace every file and directory name with a short salted hash
(`d-3fa2c1` for a directory, `f-91c0aa` plus the file's extension when it is short and
plain), keeping the shape of the tree. If the
script reports that its redaction self-check failed, nothing was written: report it,
never work around it.

## Exits

- 1 — a bad argument, an unreadable path, or a refused `--out`; relay the message.
- 3 — nothing to draw (every file excluded, or an empty tree); show the command and the
  excludes, and offer to loosen them.

## Rules

- Read-only: never write the tree being drawn, and never write inside the vault.
- `--redact` before anything leaves the user's own screen, every time.
