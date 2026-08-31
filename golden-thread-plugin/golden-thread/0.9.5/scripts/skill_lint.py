#!/usr/bin/env python3
"""
skill_lint.py — Audit skill descriptions against the composition rules.

Two rules, per the golden-thread ADR-1 (adopted from the Mindforge design doc):

  1. Skills compose through FILES, never through each other. A skill reads shared
     artifacts and writes its own; it never requires another skill to have run.
     Remove any one skill and the rest keep working.

  2. No two skills may plausibly fire on the same intent. Trigger overlap is
     checked whenever a skill is added or edited.

Rule 2 is what this script mechanises. Mindforge checks it by hand; a description
is the trigger, so an overlap means the wrong skill fires and the user cannot tell
why. Copy-pasting a sibling's description is the way it happens — that is exactly
how gt-refresh ended up claiming "is the wiki still current".

Rule 1 is only partly checkable: pointing at another skill is fine ("if the vault
is not configured, tell the user to run /gt:gt-init"), while *requiring* one to
have run is not. The difference is intent, so this reports candidates rather than
failures and leaves the judgement to a reader.

Usage:
    python3 skill_lint.py <plugin-root> [<plugin-root> ...]
    python3 skill_lint.py .          # auto-discovers skills/ directories beneath

Exit codes:
    0 = no trigger collisions
    2 = at least one collision (rule 2 violated)
"""
import itertools
import re
import sys
from pathlib import Path

# Phrases too generic to count as a distinguishing trigger on their own.
STOPWORDS = {
    "check the wiki", "check the vault", "help", "go", "start", "run it",
}


def find_skills(roots):
    """Return {skill_name: (description, path)} for every SKILL.md beneath roots."""
    out = {}
    for root in roots:
        for skill_md in sorted(Path(root).rglob("skills/*/SKILL.md")):
            head = skill_md.read_text(encoding="utf-8")[:4000]
            m = re.search(r'^description:\s*"(.*?)"\s*$', head, re.S | re.M)
            desc = (m.group(1) if m else "").replace("\n", " ").strip()
            out[skill_md.parent.name] = (desc, skill_md)
    return out


def triggers(desc):
    """Trigger phrases a description advertises, normalised for comparison."""
    m = re.search(r"Use when(?: the user says)?:\s*(.+?)(?:\.\s|\.$|$)", desc)
    if not m:
        return set()
    parts = re.split(r",|;", m.group(1))
    return {
        p.strip().lower().rstrip(".")
        for p in parts
        if len(p.strip()) > 3 and p.strip().lower().rstrip(".") not in STOPWORDS
    }


def main(argv):
    roots = argv[1:] or ["."]
    skills = find_skills(roots)
    if not skills:
        print(f"No SKILL.md files found under: {', '.join(roots)}")
        return 0

    trig = {name: triggers(desc) for name, (desc, _) in skills.items()}

    collisions = []
    for a, b in itertools.combinations(sorted(trig), 2):
        shared = trig[a] & trig[b]
        if shared:
            collisions.append((a, b, sorted(shared)))

    missing = [n for n, t in trig.items() if not t]

    print(f"Checked {len(skills)} skills across {len(roots)} root(s).\n")

    if collisions:
        print(f"TRIGGER COLLISIONS — {len(collisions)} pair(s) violate rule 2:\n")
        for a, b, shared in collisions:
            print(f"  {a}  <->  {b}")
            for s in shared:
                print(f"      shared trigger: \"{s}\"")
            print("      fix: give each a distinct vocabulary, and have each name the")
            print("           other as the alternative so a reader is redirected rather")
            print("           than left guessing which fired.\n")
    else:
        print("No trigger collisions. Rule 2 holds.\n")

    if missing:
        print("No advertised triggers (cannot be checked for overlap):")
        for n in sorted(missing):
            print(f"  {n}")
        print()

    print("Rule 1 (compose through files, never through each other) is not")
    print("mechanically checkable and is not asserted here. Referring a user to")
    print("another skill is fine; requiring one to have run is not.")

    return 2 if collisions else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
