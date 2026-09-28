#!/usr/bin/env python3
"""Weekly vault lint. Runs gt_lint.py and, if present, the gt-wiki wiki_lint.py against the vault
named in ~/.claude/vault-config.json, writes the report to <report dir>/latest.md (history as <date>.log,
unscanned), and drops ONE line into INBOX.md when there is anything to act on -- so the next session sees
it in the TASKS rollup's Inbox section.

The report dir is `lint_report_dir` in vault-config.json (absolute, or relative to the vault); else
<vault>/Projects/golden-thread/lint if that folder already exists (every release before 0.13.0 wrote
there, and an upgrade must not move it); else <vault>/.gt/lint. When the gt-wiki plugin is not installed the report and the log say
"wiki_lint: not installed" rather than leaving the reader to wonder whether it ran.

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

DEFAULT_REPORT_DIR = os.path.join(".gt", "lint")
# Where every release before 0.13.0 wrote the report. An upgraded vault that already has
# it keeps using it: an upgrade must converge on the new release without silently moving
# a user's report to a folder they have never seen (owner requirement, 2026-09-14).
LEGACY_REPORT_DIR = os.path.join("Projects", "golden-thread", "lint")


def _version_key(path):
    # .../gt-wiki/<version>/scripts/wiki_lint.py -> numeric sort, so 0.10.0 beats 0.9.0
    ver = os.path.basename(os.path.dirname(os.path.dirname(path)))
    try:
        return (1, tuple(int(x) for x in ver.split(".")))
    except ValueError:
        return (0, ())


def find_wiki_lint():
    """The newest installed gt-wiki wiki_lint.py, from whichever marketplace installed it."""
    cands = glob.glob(os.path.join(HOME, ".claude", "plugins", "cache", "*", "gt-wiki", "*", "scripts", "wiki_lint.py"))
    return max(cands, key=_version_key) if cands else None


def report_dir(vault, config):
    configured = config.get("lint_report_dir")
    if isinstance(configured, str) and configured.strip():
        return os.path.join(vault, os.path.expanduser(configured.strip()))   # absolute wins in join
    legacy = os.path.join(vault, LEGACY_REPORT_DIR)
    if os.path.isdir(legacy):
        return legacy
    return os.path.join(vault, DEFAULT_REPORT_DIR)


def main():
    cfg = os.path.join(HOME, ".claude", "vault-config.json")
    try:
        config = json.load(open(cfg))
        vault = config["vault_path"]
    except Exception as e:
        log(f"no vault-config.json ({e}); nothing to lint"); return 0
    here = os.path.dirname(os.path.abspath(__file__))
    gt_lint = os.path.join(here, "gt_lint.py")
    wiki_lint = find_wiki_lint()
    today = datetime.date.today().isoformat()
    out_dir = report_dir(vault, config)
    os.makedirs(out_dir, exist_ok=True)
    rel_out = os.path.relpath(out_dir, vault)
    shown_dir = out_dir if rel_out.startswith("..") else rel_out

    sections = []
    counts = {}
    r = subprocess.run([sys.executable, gt_lint, vault], capture_output=True, text=True)
    sections.append(("gt_lint.py", r.stdout + r.stderr))
    for m in re.finditer(r"^\s+([a-z-]+): (\d+)\s*$", r.stdout, re.M):
        counts[m.group(1)] = int(m.group(2))
    if wiki_lint:
        w = subprocess.run([sys.executable, wiki_lint, vault], capture_output=True, text=True)
        sections.append(("wiki_lint.py", w.stdout + w.stderr))
        m = re.search(r"Findings: (\d+)", w.stdout)
        if m and int(m.group(1)):
            counts["wiki-lint"] = int(m.group(1))
    else:
        sections.append(("wiki_lint.py", "wiki_lint: not installed (the gt-wiki plugin was not found; nothing to run)"))
        log("wiki_lint: not installed")
    total = sum(counts.values())

    report = [f"# Weekly vault lint — {today}", "",
              f"Generated {datetime.datetime.now():%Y-%m-%d %H:%M} by `gt_lint_weekly.py`. Findings: **{total}**"
              + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])) + ")" if counts else ""),
              "", "Fix at the source (the file the finding names), or suppress a false positive in `lint-declines.md`. Regenerate with `/gt:gt-lint`.", ""]
    for name, body in sections:
        report += [f"## {name}", "", "```", body.strip(), "```", ""]
    text = "\n".join(report)
    # The report quotes finding strings, which contain [[wikilinks]] that may be broken by
    # definition; if the report were scanned by the next run it would report itself. History
    # goes to a .log (not scanned), and the single Markdown copy `latest.md` is declared to the
    # vault linter in lint-declines.md (`suppress: latest.md`).
    dated = os.path.join(out_dir, f"{today}.log")
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
            + f") — report `{os.path.join(shown_dir, 'latest.md')}` (history: `{today}.log`); fix at source or decline in `lint-declines.md` [project:: golden-thread] [since:: {today}]\n")
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
