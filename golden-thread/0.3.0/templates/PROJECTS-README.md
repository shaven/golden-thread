# Projects

Master list. One line per project — open one with `/gt:gt-open <slug>`.

Shared references: [[CONVENTIONS]] · [[PROTOCOL]] · [[INFRASTRUCTURE]]

## Live views

These render in Obsidian only (requires the Dataview plugin). They read the
`domain` / `stage` / `tags` properties in each project's `README.md`
frontmatter — see [[CONVENTIONS]].

They are **inert when read as raw markdown**, which is why the static table below
is kept: that is what `/gt:gt-open` and any non-Obsidian reader actually sees.
Don't delete it.

### By domain

```dataview
TABLE WITHOUT ID
  link(file.folder, slug) AS Project,
  stage AS Stage,
  topology AS Topology,
  join(tags, ", ") AS Tags
FROM "Projects"
WHERE type = "project" AND file.name = "README"
GROUP BY domain
SORT domain ASC, stage ASC
```

### Active work only

```dataview
TABLE WITHOUT ID
  link(file.folder, slug) AS Project, domain AS Domain, topology AS Topology
FROM "Projects"
WHERE type = "project" AND file.name = "README"
  AND stage != "archived" AND stage != "superseded"
SORT domain ASC, slug ASC
```

### Everything on a given host

Change the host to re-filter. Reads each project's `source.md`, so it answers
"what would I break if this box went away?"

```dataview
LIST
FROM "Projects"
WHERE file.name = "source" AND contains(file.content, "HOSTNAME")
```

## All projects

<!-- Static list — authoritative for non-Obsidian readers. Keep in sync.
     vault_init.py appends new projects here automatically. -->

| Project | Slug | Domain | Stage | What it is |
|---|---|---|---|---|

## Layout

Every project folder holds the same files:

| File | Role |
|---|---|
| `README.md` | Status board — frontmatter properties, stage, next action |
| `source.md` | Where the code lives, which hosts, and the deploy plan |
| `idea.md` | Original brain dump — immutable |
| `research.md` | Append-only dated findings |
| `decisions.md` | Append-only numbered ADRs |
| `design.md` | Current architecture — always describes now |
| `memory/MEMORY.md` | Index of session memory files |
| `memory/*.md` | The notes themselves — **loaded on demand, not at open** |
