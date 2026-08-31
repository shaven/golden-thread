# Lint Suppressions

Findings deliberately declined. One `suppress:` line each, **with the reason** —
a suppression without a reason is indistinguishable from a bug nobody fixed.

Paths are matched case-insensitively against the finding's path, and a bare
filename suppresses that file everywhere.

## Template placeholders (shipped with the vault)

These files explain wikilink *syntax*, so they contain `[[...]]` as subject
matter rather than as links. Remove a line here if you later delete the
explanatory text.

suppress: index.md
suppress: CLAUDE.md
suppress: Projects/CONVENTIONS.md

## Immutable sources

`Sources/` files are never edited, so their links point at the world as it was
when they were captured. Repointing them would defeat having an immutable record.
Add specific source files here as you archive them.

<!-- suppress: Sources/YYYY-MM-DD Some Source.md -->

## Your declines

<!-- Add below, each with a one-line reason above it. -->
