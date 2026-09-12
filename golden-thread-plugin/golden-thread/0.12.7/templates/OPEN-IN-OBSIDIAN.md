# Opening this vault in Obsidian

**Obsidian is optional.** This folder is plain markdown and git — Claude Code reads and
writes it with no other software installed, and everything works without ever opening
Obsidian. Obsidian is for *you*: reading, searching and following links by hand.

## There is no import step

Obsidian has no "open file to import a vault" — a vault **is** a folder. So:

1. Install Obsidian from <https://obsidian.md> (free; macOS, Windows, Linux).
2. Open it and choose **"Open folder as vault"**.
3. Select this folder:

   ```
   {{VAULT_PATH}}
   ```

4. Obsidian will offer to trust the folder. It is your own folder, so yes.

That is the whole setup. Obsidian writes its own settings into `.obsidian/` inside this
folder and changes nothing else.

## Plugins worth having, and why

Only the first one actually matters for how this vault is written.

| Plugin | Type | Why this vault specifically |
|---|---|---|
| **Dataview** | community | Tasks carry inline fields — `[p:: 1] [waiting:: agent] [due:: 2026-01-31]`. That is Dataview's syntax. Without it you still see the text; with it you can query and sort by priority, owner and due date. |
| **Obsidian Git** | community | This vault is a git repo. The plugin commits and pushes on a timer, so edits you make in Obsidian do not sit uncommitted while a Claude session works in the same tree. |
| **Backlinks / Outgoing links** | core | Already built in, on by default. The `[[wikilinks]]` between Knowledge pages, decisions and projects are the point of the structure. |
| **Graph view** | core | Built in. Useful once `Knowledge/` has enough pages to show clusters. |

Community plugins install through **Settings → Community plugins → Browse**. You will
have to **turn off Restricted Mode** once; Obsidian asks first, which is correct of it.

### What you do not need

Templater, Tasks, Kanban and the rest are fine plugins that this vault does not assume.
Nothing here depends on them, and none of the files are written to their formats.

## Two conventions to know before you edit by hand

- **`TASKS.md`, `log.md` and every `decisions.md` are GENERATED.** They are rendered
  from per-session files under `Projects/golden-thread/spool/`, so an edit typed into
  Obsidian is lost at the next merge. Add a decision with `tools/gt_adr.py`, a log line
  with `tools/gt_log.py`. Every other file is yours to edit freely.
- **Two writers, one folder.** If a Claude session is working while you edit, you are
  both in one working tree. `Projects/golden-thread/sessions/` records who is live; git
  is what makes a mistake recoverable.

## Where to start reading

| File | What it is |
|---|---|
| `index.md` | the navigational index of everything |
| `Projects/CONVENTIONS.md` | how projects are structured and prioritised |
| `Projects/PROTOCOL.md` | how knowledge moves up from a session note to a shared page |
| `TASKS.md` | every open task across projects, ranked |
