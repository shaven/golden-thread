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

A third check (0.20.0): a skill that declares `context: fork` runs in a forked subagent with
none of the conversation, in the background by default (code.claude.com/docs/en/skills, "Run
skills in a subagent") -- it cannot stop and ask the owner. A forked skill whose body asks the
user anything is refused. gt-ingest, gt-lint, gt-validate, gt-optimize and gt-review were weighed
for fork in 0.20.0 and none forks: four ask the owner mid-run (ingest's stops and slug, lint's
approval loop, optimize's judgement list, review's routing), and gt-validate takes its claim
from the conversation a fork does not see -- it already hands the checking to a fresh agent.

Exit codes:
    0 = no trigger collisions
    2 = at least one collision (rule 2 violated), a skill declaring a model_intent
        outside fast|balanced|deep (0.18.1), or a forked skill that asks the owner (0.20.0)
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
            out[skill_md.parent.name] = (read_description(head), skill_md)
    return out


def read_description(head):
    """The frontmatter description: double-quoted, single-quoted or a plain scalar."""
    m = re.search(r'^description:[ \t]*"(.*?)"\s*$', head, re.S | re.M)
    if m:
        return m.group(1).replace("\n", " ").strip()
    m = re.search(r"^description:[ \t]*'((?:[^']|'')*)'\s*$", head, re.S | re.M)
    if m:
        return m.group(1).replace("''", "'").replace("\n", " ").strip()
    # Plain scalar (or a > / | block): the rest of the line plus indented continuations.
    m = re.search(r"^description:[ \t]*(\S.*(?:\n[ \t]+\S.*)*)$", head, re.M)
    if not m:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"^[>|][-+]?", "", m.group(1))).strip()


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
    # --require-intent (0.19.1): every skill must declare a model_intent. dev/release-check.sh
    # passes it for shipped plugins; a vault's own skills are not held to it.
    require_intent = "--require-intent" in argv[1:]
    roots = [a for a in argv[1:] if a != "--require-intent"] or ["."]
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

    # model_intent (0.18.1): a skill may say what kind of thinking it needs, never which
    # model -- and only in the closed vocabulary gt_model.py resolves. An unknown value is
    # refused HERE, at the gate, not discovered when the skill runs.
    bad_intents = model_intent_problems(skills)
    if bad_intents:
        print("UNKNOWN model_intent VALUES — refused:\n")
        for b in bad_intents:
            print("  " + b)
        print()

    forked = fork_problems(skills)
    if forked:
        print("FORKED SKILLS THAT ASK THE OWNER — refused:\n")
        for f in forked:
            print("  " + f)
        print()

    print("Rule 1 (compose through files, never through each other) is not")
    print("mechanically checkable and is not asserted here. Referring a user to")
    print("another skill is fine; requiring one to have run is not.")

    silent = []
    if require_intent:
        silent = missing_intents(skills)
        if silent:
            print("NO model_intent DECLARED — refused (--require-intent):\n")
            for s in silent:
                print("  " + s)
            print()

    return 2 if (collisions or bad_intents or silent or forked) else 0


# A step that waits on the person: any of these in a forked skill's body is a refusal.
ASKS_OWNER = re.compile(r"AskUserQuestion|\bask (the )?(user|owner)\b|\bAsk[: ]+[\"\u201c]|"
                        r"\bAsk yes/no\b|\bapproval loop\b|\bwith the user\b|"
                        r"\bask the owner first\b|\bwait for the (owner|user)\b", re.I)


def fork_problems(skills):
    """-> [str] for every skill that declares `context: fork` and also talks to the owner."""
    out = []
    for name, (_desc, path) in sorted(skills.items()):
        text = Path(path).read_text(encoding="utf-8")
        lines = text.split("\n")
        if not lines or lines[0].strip() != "---":
            continue
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is None:
            continue
        if not any(re.match(r"^context:[ \t]*fork[ \t]*$", l) for l in lines[1:end]):
            continue
        for n, l in enumerate(lines[end + 1:], end + 2):
            if ASKS_OWNER.search(l):
                out.append("%s:%d: context: fork, but the skill asks the owner mid-run (%r); a "
                           "forked skill runs without the conversation, in the background"
                           % (path, n, l.strip()[:80]))
                break
    return out


def missing_intents(skills):
    """-> [path] of every skill whose frontmatter declares no model_intent."""
    import importlib.util
    here = Path(__file__).resolve().parent / "gt_model.py"
    spec = importlib.util.spec_from_file_location("gt_model_for_lint", str(here))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return sorted(str(p) + ": no model_intent (fast | balanced | deep)"
                  for _d, p in skills.values() if mod.read_intent(p)[0] is None)


def model_intent_problems(skills):
    """-> [str] naming file:line and the value for every model_intent outside the vocabulary.
    The vocabulary and the reader are gt_model.py's, imported rather than copied."""
    import importlib.util
    here = Path(__file__).resolve().parent / "gt_model.py"
    try:
        spec = importlib.util.spec_from_file_location("gt_model_for_lint", str(here))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:                                  # noqa: BLE001
        return ["model_intent could not be checked: gt_model.py unavailable (%s)" % exc]
    return mod.check_paths([path for _desc, path in skills.values()])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
