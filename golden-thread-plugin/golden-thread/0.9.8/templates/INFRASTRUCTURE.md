# {{DOMAIN}} Infrastructure

The canonical server fleet. Defined **once** here and referenced by wikilink
from each project's `source.md` — never copied into a project, because copies
drift.

Update this file when a machine is added, retired, or changes role. Projects
that pin a path affected by such a change are found with `/gt:gt-lint`.

## Environments

| Tier | Meaning |
|---|---|
| `prod` | Serves real traffic. Changes here need a deliberate deploy. |
| `staging` | Pre-production mirror of `prod`. |
| `dev` | Development mirror of `prod`. Safe to break. |
| `dev-support` | Tooling boxes that serve no site — build agents, headless browsers, data jobs. |

## Fleet

<!--
Addressing is role × env → host + path. "Role" is the logical service, so the
same role appears once per environment. "Host" is the SSH alias as configured
in ~/.ssh/config.
-->

| Role | Env | Host | Address | Site | Purpose |
|---|---|---|---|---|---|
|  |  |  |  |  |  |

## Connection

<!--
How the control machine reaches these hosts. Note whether access is direct or
via a jump host, and name the jump host if there is one. Record credential
LOCATIONS, never credential values.
-->

## Retired

<!--
Machines no longer in use, and what replaced them. Kept because older notes and
Sources/ entries still reference them by name — this table is what tells a
future reader that a hostname they just read is dead.
-->

| Host | Retired | Replaced by | Notes |
|---|---|---|---|
|  |  |  |  |
