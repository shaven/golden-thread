#!/usr/bin/env python3
"""Every count and version number in the docs, derived from the source and compared.

    python3 dev/check_doc_counts.py            # exit 1 and name each mismatch

WHY THIS IS A GATE STEP AND NOT A HABIT

A count is quoted in several documents at once, so one stale number becomes four, and nothing
about a wrong number looks wrong. On 2026-09-16 a single pass found `gt Skills (16)` in two
documents while 21 shipped, a version line naming five modules at 0.15.0 after all of them had
moved to 0.16.0, and gt-lint's check count given as 18 in the docs and 13 in CLAUDE.md. Each
had been true when written. None was true any more, and the release gate was green throughout,
because it checks that a skill is NAMED in three files, never that what they say is so.

The rule the docs carry -- "never write a count you have not derived" -- is exactly the kind of
discipline this project has learned not to trust. So the count is derived here instead, and a
document that disagrees with the code fails the build.

WHAT IT DOES NOT DO. It checks numbers, which are the mechanical part. A paragraph that is
accurate about a previous release still passes; nothing here can catch that.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)                       # golden-thread-plugin/
ROOT = os.path.dirname(REPO)                       # the repo itself


def newest_version(plugin_dir):
    """-> the highest x.y.z directory under a plugin, the one that ships."""
    path = os.path.join(REPO, plugin_dir)
    if not os.path.isdir(path):
        return None
    vers = [d for d in os.listdir(path) if re.match(r"\A\d+\.\d+\.\d+\Z", d)]
    if not vers:
        return None
    return max(vers, key=lambda v: tuple(int(x) for x in v.split(".")))


def read(rel):
    try:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def derive():
    """Every number this file knows how to establish from the source."""
    facts = {}
    gt = newest_version("golden-thread")
    facts["gt_version"] = gt
    skills = os.path.join(REPO, "golden-thread", gt, "skills")
    facts["gt_skills"] = len([d for d in os.listdir(skills)
                              if os.path.isdir(os.path.join(skills, d))])
    rules = os.path.join(REPO, "golden-thread", gt, "templates", "core-rules")
    # The priority MODEL is not itself a rule; it describes how the rules rank.
    facts["core_rules"] = len([f for f in os.listdir(rules)
                               if f.startswith("core_") and f.endswith(".md")
                               and "priority_model" not in f])
    # gt_lint's checks, taken from what the code EMITS rather than from its own prose: the
    # docstring listed 17 while 18 were emitted, and CLAUDE.md said 13 (2026-09-16).
    lint = os.path.join(REPO, "golden-thread", gt, "scripts", "gt_lint.py")
    try:
        with open(lint, encoding="utf-8") as fh:
            facts["lint_checks"] = len(set(re.findall(r'"check":\s*"([a-z0-9-]+)"', fh.read())))
    except OSError:
        facts["lint_checks"] = None
    for mod in ("golden-thread-wiki", "golden-thread-demo", "golden-thread-watch",
                "golden-thread-report-card", "golden-thread-farm", "golden-thread-flow"):
        v = newest_version(mod)
        if v:
            facts[mod] = v
    return facts


WORDS = {16: "Sixteen", 17: "Seventeen", 18: "Eighteen", 19: "Nineteen", 20: "Twenty",
         21: "Twenty-One", 22: "Twenty-Two", 23: "Twenty-Three", 24: "Twenty-Four",
         9: "Nine", 10: "Ten", 11: "Eleven", 12: "Twelve", 13: "Thirteen"}


def check(facts):
    """-> list of (file, what is wrong). Each check names the document and the true value."""
    bad = []
    n = facts["gt_skills"]

    s = read("golden-thread-plugin/golden-thread-docs.md")
    if s is not None:
        m = re.search(r"^## gt Skills \((\d+)\)", s, re.M)
        if not m:
            bad.append(("golden-thread-docs.md", "no `## gt Skills (N)` heading to check"))
        elif int(m.group(1)) != n:
            bad.append(("golden-thread-docs.md",
                        "says %s skills; %d ship" % (m.group(1), n)))
        # The version line names six plugins whose numbers move independently.
        line = re.search(r"^## Version .*$", s, re.M)
        if not line:
            bad.append(("golden-thread-docs.md", "no `## Version ...` line to check"))
        else:
            for key, want in facts.items():
                if not key.startswith("golden-thread-"):
                    continue
                short = key.replace("golden-thread-", "gt-")
                if short in line.group(0):
                    near = re.search(re.escape(short) + r"[^0-9]{0,40}(\d+\.\d+\.\d+)",
                                     line.group(0))
                    if near and near.group(1) != want:
                        bad.append(("golden-thread-docs.md",
                                    "version line says %s %s; %s ships"
                                    % (short, near.group(1), want)))

    s = read("golden-thread-plugin/README.md")
    if s is not None:
        m = re.search(r"^## ([A-Z][a-z]+(?:-[A-Z][a-z]+)?) Skills", s, re.M)
        if m and WORDS.get(n) and m.group(1) != WORDS[n]:
            bad.append(("golden-thread-plugin/README.md",
                        "heading says %s Skills; %d (%s) ship"
                        % (m.group(1), n, WORDS.get(n, n))))

    for rel in ("README.md", "golden-thread-plugin/golden-thread-docs.md"):
        s = read(rel)
        if s is None:
            continue
        # Only a NUMBER may be wrong here. Matching any word before "Core rules" flagged
        # "The Core rules" and "backed Core rules" -- a checker that cries wolf gets muted,
        # which would leave the real drift unwatched.
        numbers = "|".join(list(WORDS.values()) + [str(k) for k in WORDS])
        want = WORDS.get(facts["core_rules"], str(facts["core_rules"]))
        for m in re.finditer(r"\b(%s) Core rules\b" % numbers, s, re.I):
            word = m.group(1)
            if word.casefold() not in (want.casefold(), str(facts["core_rules"])):
                bad.append((rel, "says %r Core rules; %d ship"
                            % (word, facts["core_rules"])))
                break
    n_lint = facts.get("lint_checks")
    if n_lint:
        want = {str(n_lint), WORDS.get(n_lint, "").casefold()}
        for rel in ("README.md", "golden-thread-plugin/golden-thread-docs.md", "CLAUDE.md"):
            s = read(rel)
            if s is None:
                continue
            numbers = "|".join(list(WORDS.values()) + [str(k) for k in WORDS])
            m = re.search(r"\b(%s)\s+(?:deterministic\s+)?checks?\b" % numbers, s, re.I)
            if m and m.group(1).casefold() not in want:
                bad.append((rel, "says %r gt_lint checks; %d are emitted"
                            % (m.group(1), n_lint)))
    return bad


def main():
    facts = derive()
    bad = check(facts)
    print("derived from the source: gt %s, %d skills, %d Core rules, %s lint checks"
          % (facts["gt_version"], facts["gt_skills"], facts["core_rules"],
             facts.get("lint_checks")))
    for f, why in bad:
        print("  %-38s %s" % (f, why))
    if bad:
        print("\n%d doc count(s) disagree with the code. Fix the DOCUMENT -- the code is the "
              "source of truth for a count." % len(bad))
        return 1
    print("every derivable count in the docs matches the code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
