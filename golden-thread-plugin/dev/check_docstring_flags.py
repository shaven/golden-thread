#!/usr/bin/env python3
"""A flag a tool's usage block advertises must be a flag argparse actually has.

    dev/check_docstring_flags.py <version-dir> [...]     # exit 0 = every advertised flag is real

## Why this is a release gate

2026-09-28: `gt_state.py`'s usage block read `gt_state.py write [--reason R] [--force]`. The
`write` subparser has never had `--force`, and `check` has a `--write` flag the block never
showed. Nobody noticed for the life of the file, because a docstring is not executed. It was
found by an agent sent to DOCUMENT the tool -- which is to say, by the one activity that reads
a usage block closely, and which happens once per release if it happens at all.

That is the same shape as the defect `check_cli_contract.py` exists for, one level out: there,
a rule told callers to pass a flag that did not exist; here, a tool's own front page does. A
person who types the advertised flag gets `error: unrecognized arguments` and has no way to
know whether the tool or the documentation is wrong.

## The contract

For every `gt_*.py` in `scripts/` with an `argparse.ArgumentParser`:

  * every `--flag` on a USAGE LINE -- a docstring line whose first token is the script's own
    name -- must be accepted;
  * when that usage line names a subcommand, the flag must be accepted by THAT subcommand, not
    merely by some other one. A flag that exists on a sibling verb is exactly as unusable as
    one that does not exist.

**Only usage lines, and this is not a detail.** A docstring discusses flags in prose all the
time, and the discussion is often about a flag that deliberately does NOT exist:
`gt_allin_commit.py` says "There is no --push flag", and `gt_schedule.py` explains a standing
rule about scripts having "--check". A first version of this gate read every `--flag` anywhere
in the docstring and reported both as defects -- three of its five findings were prose. A check
that calls a correct thing wrong is worse than no check, because people stop reading it, and
this release had already met that failure twice in `gt_daily --check` and `gt_schedule check`.
A usage line is a claim about how to invoke the tool. Prose is not.

Like `check_cli_contract.py`, this RUNS `--help` rather than reading the source. A flag
assembled by `parents=` or added in a branch is still a real flag, and a flag written into an
`add_argument` call that never reaches the parser is still not one.

`EXEMPT` names docstrings that mention a flag belonging to a DIFFERENT tool -- a cross
reference, not a claim about itself. Each entry says which flag and why.
"""
import concurrent.futures
import os
import re
import subprocess
import sys

PY = os.environ.get("GT_PYTHON", sys.executable or "python3")

# script -> flags a USAGE LINE names that belong to something else. Restricting the gate to
# usage lines removed every exemption there was; the map stays because a tool whose usage block
# legitimately shows another tool's invocation is a thing that will happen, and an exemption is
# a decision on the record rather than an omission.
EXEMPT = {}

# `--json`-style tokens. Deliberately not matching a bare `--` or a `---` rule line.
FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]+)")
# A USAGE LINE: indented, and its first token is a gt script's name.
USAGE_LINE = re.compile(r"^\s+(gt_[a-z_]+\.py)\b(.*)$")
# Subcommand names, from BOTH sources, because each misses cases the other catches.
# `add_parser("x")` finds a verb argparse wrapped or truncated in its choice list; the choice
# list finds a verb registered in a LOOP, which is how gt_schedule.py registers three of its
# four and which a source regex cannot see. Taking the union was the difference between this
# gate reporting four real flags as missing and reporting nothing.
ADD_PARSER = re.compile(r"""add_parser\(\s*["']([a-z][a-z0-9-]*)["']""")
# The SUBPARSER choice list only: argparse prints it alone on its own line under "positional
# arguments". Matching every `{a,b,c}` in the help text also caught option metavars such as
# `--usage-alert {early,normal,late}`, and the gate then spawned a `--help` per fake verb --
# hundreds of processes, minutes of wall clock, for answers it could not use.
CHOICES = re.compile(r"^\s*\{([a-z0-9][a-z0-9,_-]*)\}\s*$", re.M)


def helptext(path, *args):
    try:
        p = subprocess.run([PY, str(path), *args, "--help"], capture_output=True, text=True,
                           timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout + p.stderr


def docstring(src):
    m = re.match(r'(?s)\A(?:#![^\n]*\n)?(?:from __future__[^\n]*\n)?\s*"""(.*?)"""', src)
    return m.group(1) if m else ""


def check(version_dir):
    problems = []
    scripts = sorted((version_dir + "/scripts/" + n)
                     for n in os.listdir(version_dir + "/scripts")
                     if n.startswith("gt_") and n.endswith(".py"))
    for path in scripts:
        name = os.path.basename(path)
        try:
            src = open(path, encoding="utf-8").read()
        except OSError:
            continue
        if "argparse.ArgumentParser" not in src:
            continue
        doc = docstring(src)
        if not doc:
            continue
        exempt = EXEMPT.get(name, set())
        top = helptext(path)
        if not top:
            problems.append("%s: --help produced nothing, so nothing could be verified" % name)
            continue
        verbs = set(ADD_PARSER.findall(src))
        for group in CHOICES.findall(top):
            verbs.update(v for v in group.split(",") if v)
        # One `--help` per verb, run together. Serially this gate took 90s on 24 scripts,
        # which is long enough that someone eventually runs the release check without it.
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            verb_help = dict(zip(sorted(verbs),
                                 pool.map(lambda v: helptext(path, v), sorted(verbs))))
        anywhere = top + "".join(verb_help.values())

        seen = set()
        for line in doc.splitlines():
            m = USAGE_LINE.match(line)
            if not m or m.group(1) != name:
                continue
            rest = m.group(2)
            # The verb this line is about, if it names one. Tokens before it may be global
            # flags or their values, as in `gt_registry.py [--vault V] show <slot> [--json]`.
            verb = next((tok.strip("[]<>()") for tok in rest.split()
                         if tok.strip("[]<>()") in verbs), None)
            for flag in FLAG.findall(rest):
                if flag in exempt or (verb, flag) in seen:
                    continue
                seen.add((verb, flag))
                if verb is not None:
                    if flag not in verb_help.get(verb, "") and flag not in top:
                        problems.append("%s: the usage block advertises `%s %s` and neither "
                                        "that subcommand nor the tool itself accepts it"
                                        % (name, verb, flag))
                elif flag not in anywhere:
                    problems.append("%s: the usage block advertises %s and nothing accepts it"
                                    % (name, flag))
    return problems


def main(argv):
    if not argv:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    problems = []
    for d in argv:
        problems += check(d.rstrip("/"))
    for p in problems:
        print("  " + p)
    if problems:
        print("docstring flags: %d problem(s)" % len(problems))
        return 1
    print("every flag a usage block advertises is a flag argparse has")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
