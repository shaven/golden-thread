#!/usr/bin/env python3
"""Every tool that can write a vault must be told which vault, and be rehearsable.

    dev/check_cli_contract.py <version-dir> [...]     # exit 0 = contract held

## Why this is a release gate and not a convention

2026-09-11: a session rehearsing the 0.11.0 migration copied the vault to a scratch
directory and ran every migration there. `gt_log.py` took `--vault`, so it obeyed.
`gt_adr.py migrate` did not, so it read `~/.claude/vault-config.json` and migrated
the LIVE vault's 42 `decisions.md` files instead.

The Core rule `core_explicit_vault_target` tells the caller to pass `--vault`. A rule
to pass a flag that does not exist is unfollowable, so the tools must offer it --
and "must" in a document decays. Here it fails the build instead.

## The contract

For every tool listed in `COVERED`:
  * `--vault` is accepted, so the target can be stated;
  * `--dry-run` is accepted, so it can be rehearsed;
  * both work AFTER the subcommand as well as before, because that is where people
    type them (argparse `parents=` makes this easy to get wrong — and a flag that
    silently does not parse is worse than one that is missing).

`EXEMPT` names tools that are deliberately outside it, each with a reason. An
exemption is a decision on the record, not an omission.

The check RUNS each tool with `--help` rather than reading its source: a flag that
argparse does not actually expose is exactly the failure being guarded against, and
source inspection would have passed the 0.12.0 bug where the flag parsed only before
the subcommand.
"""
import os
import subprocess
import sys

PY = os.environ.get("GT_PYTHON", sys.executable or "python3")

# tool (relative to the version dir) -> subcommands that write, or () for "no
# subcommands: the tool writes however it is invoked".
COVERED = {
    "templates/tools/gt_adr.py": ("allocate", "merge", "migrate"),
    "templates/tools/gt_log.py": ("add", "merge", "migrate"),
    "templates/tools/gt_tasks.py": (),
    "templates/tools/gt_session.py": ("register", "claim", "release"),
    "scripts/vault_init.py": ("fresh", "create-project", "connect", "rename-project",
                              "merge-project", "archive-project", "install-core-rules"),
}

EXEMPT = {
    "templates/tools/safe_write.py":
        "a file writer, not a vault tool: it is handed a path and has no vault to infer",
    "templates/tools/gt_edits.py":
        "per-working-tree attribution; it records into .git, never into vault files",
    "templates/tools/gt_spool.py":
        "library for gt_log/gt_adr; its CLI is diagnostic only and writes nothing",
    "templates/tools/gt_closeout.py":
        "reads log.md and asks questions; its only write is its own history file",
}


def helptext(tool, *args):
    try:
        p = subprocess.run([PY, str(tool), *args, "--help"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    return (p.stdout or "") + (p.stderr or ""), None


def check(version_dir):
    problems = []
    for rel, subs in sorted(COVERED.items()):
        tool = os.path.join(version_dir, rel)
        if not os.path.isfile(tool):
            problems.append("%s: listed in COVERED but not shipped" % rel)
            continue
        top, err = helptext(tool)
        if top is None:
            problems.append("%s: could not run --help (%s)" % (rel, err))
            continue
        # A tool with no subcommands must carry the flags at the top level. A tool
        # WITH subcommands is judged per subcommand instead: vault_init.py declares
        # --vault on each subparser deliberately (it is required there, which is
        # stronger than a global optional flag), and demanding it twice would push
        # tools towards a weaker CLI to satisfy the checker.
        if not subs:
            for flag in ("--vault", "--dry-run"):
                if flag not in top:
                    problems.append("%s: %s missing — core_explicit_vault_target requires it"
                                    % (rel, flag))
        # Where people actually type it: after the verb.
        for sub in subs:
            text, err = helptext(tool, sub)
            if text is None:
                problems.append("%s %s: could not run --help (%s)" % (rel, sub, err))
                continue
            for flag in ("--vault", "--dry-run"):
                if flag not in text:
                    problems.append(
                        "%s %s: %s does not parse after the subcommand — a flag people "
                        "believe they passed" % (rel, sub, flag))
    return problems


def main(argv):
    if not argv:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    problems = []
    for version_dir in argv:
        problems += check(version_dir)
    if problems:
        for p in problems:
            print("  " + p)
        print("CLI contract: %d problem(s)" % len(problems))
        return 1
    covered = len(COVERED)
    print("CLI contract: %d tool(s) take --vault and --dry-run, %d exempt by name"
          % (covered, len(EXEMPT)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
