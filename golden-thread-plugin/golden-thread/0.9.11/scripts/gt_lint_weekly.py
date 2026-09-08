#!/usr/bin/env python3
"""Weekly vault lint. Runs gt_lint.py and, if present, the gt-wiki wiki_lint.py against the vault
named in ~/.claude/vault-config.json, writes the report into the vault under
Projects/golden-thread/lint/<date>.md (and latest.md), and drops ONE line into INBOX.md when
there is anything to act on -- so the next session sees it in the TASKS rollup's Inbox section.

Wired by a launchd agent (see the golden-thread runbook); harmless to run by hand:

    python3 ~/.claude/golden-thread/hooks/gt_lint_weekly.py

It never commits: the next session that opens the vault commits the report with its own work.
It never writes INBOX.md while a live session holds a claim on it; the report is still written
and the skipped INBOX line is noted in the log at ~/.claude/golden-thread/lint_weekly.log.
"""
import datetime, glob, json, os, re, subprocess, sys, time

HOME = os.path.expanduser("~")
LOG = os.path.join(HOME, ".claude", "golden-thread", "lint_weekly.log")

def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as fh:
        fh.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")

def main():
    cfg = os.path.join(HOME, ".claude", "vault-config.json")
    try:
        vault = json.load(open(cfg))["vault_path"]
    except Exception as e:
        log(f"no vault-config.json ({e}); nothing to lint"); return 0
    here = os.path.dirname(os.path.abspath(__file__))
    gt_lint = os.path.join(here, "gt_lint.py")
    wiki_lints = sorted(glob.glob(os.path.join(HOME, ".claude", "plugins", "cache", "golden-thread-plugin", "gt-wiki", "*", "scripts", "wiki_lint.py")))
    today = datetime.date.today().isoformat()
    out_dir = os.path.join(vault, "Projects", "golden-thread", "lint")
    os.makedirs(out_dir, exist_ok=True)

    sections = []
    counts = {}
    r = subprocess.run([sys.executable, gt_lint, vault], capture_output=True, text=True)
    sections.append(("gt_lint.py", r.stdout + r.stderr))
    for m in re.finditer(r"^\s+([a-z-]+): (\d+)\s*$", r.stdout, re.M):
        counts[m.group(1)] = int(m.group(2))
    if wiki_lints:
        w = subprocess.run([sys.executable, wiki_lints[-1], vault], capture_output=True, text=True)
        sections.append(("wiki_lint.py", w.stdout + w.stderr))
        m = re.search(r"Findings: (\d+)", w.stdout)
        if m and int(m.group(1)):
            counts["wiki-lint"] = int(m.group(1))
    total = sum(counts.values())

    report = [f"# Weekly vault lint — {today}", "",
              f"Generated {datetime.datetime.now():%Y-%m-%d %H:%M} by `gt_lint_weekly.py`. Findings: **{total}**"
              + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])) + ")" if counts else ""),
              "", "Fix at the source (the file the finding names), or suppress a false positive in `lint-declines.md`. Regenerate with `/gt:gt-lint`.", ""]
    for name, body in sections:
        report += [f"## {name}", "", "```", body.strip(), "```", ""]
    text = "\n".join(report)
    dated = os.path.join(out_dir, f"{today}.md")
    open(dated, "w").write(text)
    open(os.path.join(out_dir, "latest.md"), "w").write(text)
    log(f"report written: {dated} findings={total} {counts}")

    if total == 0:
        return 0
    inbox = os.path.join(vault, "INBOX.md")
    if not os.path.exists(inbox):
        log("no INBOX.md; skipped inbox line"); return 0
    # respect Core rule 1: never write a file a LIVE session has claimed
    sessions = glob.glob(os.path.join(vault, "Projects", "golden-thread", "sessions", "*_*.md"))
    now = time.time()
    for sf in sessions:
        try:
            if now - os.path.getmtime(sf) < 86400 and "INBOX.md" in open(sf, errors="ignore").read():
                log("INBOX.md is claimed by a live session; inbox line skipped (report still written)"); return 0
        except Exception:
            pass
    line = (f"- [ ] Weekly vault lint {today}: **{total} findings** ("
            + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
            + f") — report `Projects/golden-thread/lint/{today}.md`; fix at source or decline in `lint-declines.md` [project:: golden-thread] [since:: {today}]\n")
    existing = open(inbox, errors="ignore").read()
    if f"Weekly vault lint {today}" in existing:
        log("inbox line for today already present"); return 0
    with open(inbox, "a") as fh:
        if not existing.endswith("\n"):
            fh.write("\n")
        fh.write(line)
    log("inbox line appended")
    return 0

if __name__ == "__main__":
    sys.exit(main())
