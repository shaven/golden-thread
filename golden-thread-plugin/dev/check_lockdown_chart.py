#!/usr/bin/env python3
"""check_lockdown_chart -- the lockdown chart in the docs must be the one the code applies (0.20.5).

    python3 dev/check_lockdown_chart.py [GT_VERSION_DIR]           check; exit 1 on any drift
    python3 dev/check_lockdown_chart.py [GT_VERSION_DIR] --write   rewrite each chart block

Every doc in DOCS carries the chart between the markers

    <!-- lockdown-chart:start -->
    ...
    <!-- lockdown-chart:end -->

and the text between them must equal `gt_lockdown.py table --markdown` from the release being
built. A doc without the markers, or with a hand-edited chart, fails release-check's docs step:
a user reads the chart to decide what they are getting into, so it may not describe a level the
code does not apply. GT_VERSION_DIR defaults to the newest golden-thread/<version>.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = ["README.md", "SECURITY.md", "MANUAL.md", "INSTALL.md"]
START, END = "<!-- lockdown-chart:start -->", "<!-- lockdown-chart:end -->"
BLOCK = re.compile(re.escape(START) + r"\n(.*?)" + re.escape(END), re.S)


def _newest_gt():
    base = os.path.join(ROOT, "golden-thread")
    vers = [d for d in os.listdir(base) if re.match(r"^\d+\.\d+\.\d+$", d)]
    vers.sort(key=lambda v: tuple(int(x) for x in v.split(".")))
    return os.path.join(base, vers[-1])


def chart(gt_dir):
    tool = os.path.join(gt_dir, "scripts", "gt_lockdown.py")
    if not os.path.isfile(tool):
        return None
    out = subprocess.run([sys.executable, "-B", tool, "table", "--markdown"],
                         capture_output=True, text=True, check=True).stdout
    return out.rstrip("\n") + "\n"


def main(argv):
    write = "--write" in argv
    args = [a for a in argv if a != "--write"]
    gt_dir = os.path.abspath(args[0]) if args else _newest_gt()
    want = chart(gt_dir)
    if want is None:
        print("no gt_lockdown.py in %s -- nothing to check" % gt_dir)
        return 0
    problems = []
    for name in DOCS:
        p = os.path.join(ROOT, name)
        text = open(p, encoding="utf-8").read()
        m = BLOCK.search(text)
        if not m:
            problems.append("%s: no lockdown chart (markers %s ... %s)" % (name, START, END))
            continue
        if m.group(1) != want:
            if write:
                text = text[:m.start(1)] + want + text[m.end(1):]
                with open(p, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
                print("rewrote the chart in %s" % name)
            else:
                problems.append("%s: lockdown chart differs from gt_lockdown.py table --markdown"
                                % name)
    for pr in problems:
        print("✗ " + pr)
    if problems:
        print("fix: add the markers where missing, then python3 dev/check_lockdown_chart.py --write")
        return 1
    print("lockdown chart matches gt_lockdown.py in %d docs" % len(DOCS))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
