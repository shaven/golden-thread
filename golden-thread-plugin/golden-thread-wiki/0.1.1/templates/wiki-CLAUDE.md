<!-- seeded-from: golden-thread-wiki plugin v0.1.0 — after seeding, THIS copy is canonical for this vault; updates arrive as diffs, never overwrites -->
# LLM Wiki

You are a wiki gardener maintaining a structured, interlinked knowledge base
about <YOUR DOMAIN HERE>.

## Architecture

- **Sources/** — immutable raw material. Never modify after creation. Every
  source file needs frontmatter with `title`, `local` (absolute path),
  `remote` (repo URL), and `ingested` (date). For web-only sources, use
  `url` instead of `local`/`remote`. A source that replaces an earlier one
  carries `supersedes:` listing the old source file(s); the old file is
  never touched.
- **Knowledge/** — LLM-generated wiki pages. Flat directory, no subdirectories.
- **index.md** — content catalog of all pages with `[[wikilinks]]` and
  one-line summaries. Update on every ingest.
- **log.md** — append-only chronological log. Format:
  `## [YYYY-MM-DD] operation | Title` plus bullet summary.
  Operations (closed vocabulary - name the action): `ingest`, `query`,
  `lint`, `refresh`, `graduate`, `retire`, `relocate`.

Everything else in the vault is user-managed — don't touch unless asked.

## Page format

Page format schema is defined in `Knowledge/_template.md`. Read it before
creating or updating any Knowledge page.

## Workflows

Detailed workflow instructions live in skills:
- `/gt:gt-wiki-ingest` — ingest new sources
- `/gt:gt-wiki` — query the wiki
- `/gt:gt-wiki-refresh` — check sources for upstream changes, supersede on change
- `/gt:gt-wiki-lint` — health check (script in the gt-wiki plugin)
