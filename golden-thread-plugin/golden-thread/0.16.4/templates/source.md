# {{TITLE}} — Source

**Topology:** {{TOPOLOGY}}
**Repo:** {{REPO_URL}}
**Fleet:** {{FLEET}}

<!--
TOPOLOGY TYPES

  local           Code is a folder on the local box. No deploy step, no servers.
                  Fill in "Deployment targets" with a single local row; delete
                  the File plan and Deploy procedure sections.

  remote          One repo, one remote server. No dev server beyond the local
                  working copy. The repo mirrors the production server's
                  structure, and the server pulls from GitHub to deploy.
                  Fill in one prod row; delete the File plan section.

  bastion-jump    Multiple servers, reached through a single gateway host.
  bastion-direct  Multiple servers, each reachable independently from the
                  control machine.

                  Both bastion variants require the File plan below. Servers
                  are typed dev-support / dev / staging / prod. Do NOT copy the
                  fleet's host table into this file — link it in "Fleet" above
                  and list only this project's paths here.
-->

## Deployment targets

<!--
Addressing is role × env → host + path, because one machine can serve several
sites. "Role" is the logical service (e.g. quote, site, worker); "Host" is the
SSH alias; "Site" is the vhost or domain, if any.
-->

| Role | Env | Host | Site | Path | Notes |
|---|---|---|---|---|---|
|  |  |  |  |  |  |

## File plan

<!--
bastion topologies only. One row per deployed file or directory.

Kind:
  static  — byte-identical on every target. Deploy from one canonical copy;
            never hand-edit on an individual host or the copies will diverge.
  unique  — a per-server variant. Never overwrite across hosts; each target
            keeps its own version.

If a file is static across some hosts and unique on others, give it one row per
group and say so in Notes.
-->

| Source (repo path) | Kind | Targets | Deployed path | Notes |
|---|---|---|---|---|
|  |  |  |  |  |

## Deploy procedure

<!--
How code actually reaches each target — git pull on the server, rsync, CI, or
manual. Include the command if there is one, and note anything that must happen
in a particular order (restart daemons, clear caches, bump versions).
-->

## Access

<!--
SSH aliases, jump host (bastion-jump only), and where credentials are stored.
Record the POINTER to credentials, never the credentials themselves.
-->
