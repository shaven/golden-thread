#!/usr/bin/env python3
"""One command for the whole health picture: `gt_doctor.py`.

    gt_doctor.py                 # every check, human-readable
    gt_doctor.py --json          # the same, machine-readable
    gt_doctor.py --fix           # apply only the repairs that cannot lose work
    gt_doctor.py --only wiring   # one check

Exit code: 0 all clear, 1 something needs attention, 2 a check could not run.

## Why one command

Everything here already existed, in five scripts and a SessionStart message: version,
component drift, hook wiring, stray workers, push state, lint. Each is correct and
each reports somewhere different, and the SessionStart message reached only the
assistant until 0.9.6 -- so "is this install healthy?" had no answer a person could
ask for directly. Worse, a clean report from a check pinned to the WRONG version
reads exactly like a clean install: on 2026-08-30 a component check aimed at 0.6.0
reported clean while 0.9.4 sat uninstalled beside it.

So this states the version every other answer is relative to, at the top, always.

## Every check answers a different question

  version     is the newest release the one installed?
  components  do the installed FILES match that release?
  wiring      is every hook the release declares actually in settings.json?
  core rules  does each rule that claims enforcement have its mechanism wired?
  vault       is the vault reachable, and are its generated files migrated?
  workers     are there background processes nobody declared?
  push        do this machine's commits exist anywhere else?
  gt-src      does the publish destination still match what was published?
  lint        what does the vault linter say, in one line?

A check that cannot run says so and exits 2. "Could not check" is never "clean" --
that distinction is the whole reason this file exists.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = Path.home() / ".claude" / "vault-config.json"
SETTINGS = Path.home() / ".claude" / "settings.json"
INSTALLED_HOOKS = Path.home() / ".claude" / "golden-thread" / "hooks"

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"
MARK = {OK: "ok   ", WARN: "WARN ", FAIL: "FAIL ", UNKNOWN: "?    "}


class Report:
    def __init__(self):
        self.rows = []

    def add(self, check, state, summary, detail="", fix=""):
        self.rows.append({"check": check, "state": state, "summary": summary,
                          "detail": detail, "fix": fix})

    @property
    def worst(self):
        for s in (FAIL, UNKNOWN, WARN):
            if any(r["state"] == s for r in self.rows):
                return s
        return OK


def run(cmd, timeout=120):
    """-> (returncode, output). Never raises: a check that dies is UNKNOWN, not clean."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)


def vault_path(explicit=None):
    if explicit:
        return Path(explicit)
    if os.environ.get("GT_VAULT"):
        return Path(os.environ["GT_VAULT"])
    try:
        return Path(json.loads(CONFIG.read_text(encoding="utf-8"))["vault_path"])
    except Exception:
        return None


def plugin_root(explicit=None):
    """Where the plugin SOURCE lives.

    Read from the wiring rather than guessed: install.sh registers gt_version_check
    with the plugin root as an argument, so settings.json already knows. A guess here
    would be the same class of error as a version check pinned to the wrong release.
    """
    if explicit:
        return Path(explicit)
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return None
    for blocks in (data.get("hooks") or {}).values():
        for b in blocks if isinstance(blocks, list) else []:
            for h in (b.get("hooks") or []) if isinstance(b, dict) else []:
                cmd = h.get("command") or ""
                if "gt_version_check.py" in cmd:
                    parts = shlex.split(cmd)
                    for i, tok in enumerate(parts):
                        if tok == "check" and i + 1 < len(parts):
                            return Path(parts[i + 1])
    return None


def version_dir(root):
    """The newest installable version directory, the way install.sh picks it."""
    if not root:
        return None
    best = None
    for d in (root / "golden-thread").glob("*/"):
        name = d.name.rstrip("/")
        if not (d / ".claude-plugin" / "plugin.json").is_file():
            continue
        try:
            key = tuple(int(x) for x in name.split("."))
        except ValueError:
            continue
        if best is None or key > best[0]:
            best = (key, d)
    return best[1] if best else None


# ---- the checks -------------------------------------------------------------

def check_version(rep, root):
    script = INSTALLED_HOOKS / "gt_version_check.py"
    if not script.is_file() or not root:
        rep.add("version", UNKNOWN, "cannot locate the plugin source",
                fix="run install.sh, then restart Claude Code")
        return
    rc, out = run([sys.executable, str(script), "check", str(root)])
    if rc is None:
        rep.add("version", UNKNOWN, "version check did not run", out)
    elif "current" in out:
        rep.add("version", OK, out.strip().splitlines()[-1].strip())
    else:
        line = next((l.strip() for l in out.splitlines() if "installed" in l), out.strip())
        rep.add("version", WARN, line,
                fix='bash "%s/install.sh"   (then restart Claude Code)' % root)


def check_components(rep, vdir):
    script = INSTALLED_HOOKS / "gt_components.py"
    if not script.is_file() or not vdir:
        rep.add("components", UNKNOWN, "gt_components.py or the release is missing")
        return
    rc, out = run([sys.executable, str(script), "check", str(vdir)])
    if rc is None:
        rep.add("components", UNKNOWN, "component check did not run", out)
        return
    # The check is pinned to the version it was handed: say which, always.
    state = OK if rc == 0 else WARN
    rep.add("components", state,
            "installed files match %s" % vdir.name if rc == 0
            else "drift against %s" % vdir.name,
            "" if rc == 0 else out.strip()[-800:],
            fix="" if rc == 0 else "python3 %s apply %s" % (script, vdir))


def check_wiring(rep, vdir):
    script = INSTALLED_HOOKS / "gt_components.py"
    if not script.is_file() or not vdir:
        rep.add("wiring", UNKNOWN, "cannot check hook wiring")
        return
    rc, out = run([sys.executable, str(script), "wiring", str(vdir)])
    if rc is None:
        rep.add("wiring", UNKNOWN, "wiring check did not run", out)
        return
    unwired = [l.strip() for l in out.splitlines() if l.strip().startswith("unwired")]
    if not unwired:
        rep.add("wiring", OK, next((l.strip() for l in out.splitlines() if "wired" in l),
                                   "every declared hook is wired"))
    else:
        rep.add("wiring", FAIL, "%d declared hook(s) not wired" % len(unwired),
                "\n".join(unwired),
                fix="python3 %s/vault_init.py install-core-rules --vault <vault>"
                    % (vdir / "scripts"))


def check_vault(rep, vault):
    if not vault:
        rep.add("vault", UNKNOWN, "no vault configured",
                fix="run /gt:gt-init, or set GT_VAULT")
        return
    if not vault.is_dir():
        rep.add("vault", FAIL, "configured vault does not exist: %s" % vault)
        return
    pending = []
    spool = vault / "Projects" / "golden-thread" / "spool"
    log = vault / "log.md"
    if log.is_file() and not (spool / "log" / "0000-baseline.md").is_file():
        pending.append("log.md is not migrated to the spool model")
    unmigrated = []
    for dec in sorted(list((vault / "Projects").glob("*/decisions.md"))
                      + list((vault / "Projects").glob("*/*/decisions.md"))):
        slug = str(dec.parent.relative_to(vault / "Projects"))
        if not (spool / "decisions" / slug / "0000-baseline.md").is_file():
            unmigrated.append(slug)
    if unmigrated:
        pending.append("%d project(s) with an unmigrated decisions.md: %s"
                       % (len(unmigrated), ", ".join(unmigrated[:5])
                          + (" …" if len(unmigrated) > 5 else "")))
    if pending:
        rep.add("vault", WARN, "%s — %d pending migration(s)" % (vault.name, len(pending)),
                "\n".join(pending),
                fix="tools/gt_log.py migrate --dry-run, then tools/gt_adr.py migrate "
                    "<project> --dry-run (drop --dry-run to apply)")
    else:
        rep.add("vault", OK, "%s — generated files migrated" % vault.name)


def check_workers(rep):
    script = INSTALLED_HOOKS / "gt_workers.py"
    if not script.is_file():
        rep.add("workers", UNKNOWN, "gt_workers.py is not installed")
        return
    rc, out = run([sys.executable, str(script), "check"])
    if rc is None:
        rep.add("workers", UNKNOWN, "worker check did not run", out)
        return
    line = out.strip().splitlines()[0] if out.strip() else "no output"
    if "clean" in out:
        rep.add("workers", OK, line.strip())
    else:
        rep.add("workers", WARN, line.strip(), out.strip()[:800],
                fix="python3 %s reap --dry-run   (then without --dry-run)" % script)


def check_push(rep):
    script = INSTALLED_HOOKS / "gt_push_check.py"
    if not script.is_file():
        rep.add("push", UNKNOWN, "gt_push_check.py is not installed")
        return
    rc, out = run([sys.executable, str(script), "check"])
    if rc is None:
        rep.add("push", UNKNOWN, "push check did not run", out)
        return
    text = out.strip() or "no output"
    state = OK if ("in sync" in text or not text.strip()) else WARN
    rep.add("push", state, text.splitlines()[-1].strip() if text else "",
            fix="" if state == OK else "git -C <vault> push")


def check_gt_src(rep):
    """Does the publish destination still hold exactly what was published?

    2026-09-11: a flat 0.9.13-era scripts/ and templates/ appeared in gt-src after a
    publish, written by something other than the sync script. The next sync deleted
    them in output that scrolled past. A foreign file in the destination is a file the
    OTHER machine may commit, so it is worth naming between syncs, not at the next one.
    """
    dest = Path(os.environ.get("GT_SRC") or
                (Path.home() / "Library/CloudStorage/OneDrive-Personal/Projects2/gt-src"))
    if not dest.is_dir():
        rep.add("gt-src", OK, "no publish destination on this machine (nothing to check)")
        return
    source_json = dest / "SOURCE.json"
    if not source_json.is_file():
        rep.add("gt-src", WARN, "%s has no SOURCE.json — provenance unknown" % dest.name,
                fix="dev/sync-gt-src.sh --dry-run")
        return
    try:
        meta = json.loads(source_json.read_text(encoding="utf-8"))
    except Exception as exc:
        rep.add("gt-src", UNKNOWN, "SOURCE.json is unreadable", str(exc))
        return
    # Top-level entries the publisher never creates are the signal worth reporting.
    expected_top = {"MANIFEST.json", "SOURCE.json", ".gitignore", "golden-thread",
                    "golden-thread-wiki", "dev", "tests", "install.sh", "selftest.sh",
                    "package.sh", "build-docs.py"}
    foreign = sorted(p.name for p in dest.iterdir()
                     if p.name not in expected_top and not p.name.endswith(
                         (".md", ".html", ".pdf", ".code-workspace")))
    if foreign:
        rep.add("gt-src", WARN,
                "%d unmanaged top-level entr%s in gt-src"
                % (len(foreign), "y" if len(foreign) == 1 else "ies"),
                "not written by sync-gt-src.sh: " + ", ".join(foreign),
                fix="inspect, then dev/sync-gt-src.sh (it backs the destination up first)")
    else:
        rep.add("gt-src", OK, "published from %s (gt %s), nothing unmanaged"
                % (str(meta.get("commit", "?"))[:9], meta.get("gt", "?")))


def check_lint(rep, vault):
    script = INSTALLED_HOOKS / "gt_lint.py"
    if not script.is_file() or not vault:
        rep.add("lint", UNKNOWN, "gt_lint.py or the vault is missing")
        return
    rc, out = run([sys.executable, str(script), str(vault)], timeout=300)
    if rc is None:
        rep.add("lint", UNKNOWN, "lint did not run", out)
        return
    findings = [l for l in out.splitlines() if l.startswith("[")]
    kinds = {}
    for f in findings:
        kinds[f.split("]")[0][1:]] = kinds.get(f.split("]")[0][1:], 0) + 1
    if not findings:
        rep.add("lint", OK, "no findings")
    else:
        top = ", ".join("%s×%d" % (k, v) for k, v in
                        sorted(kinds.items(), key=lambda kv: -kv[1])[:4])
        rep.add("lint", WARN, "%d finding(s): %s" % (len(findings), top),
                fix="/gt:gt-lint for the detail")


CHECKS = ("version", "components", "wiring", "vault", "workers", "push", "gt-src", "lint")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Golden Thread health check")
    ap.add_argument("--vault", help="vault to check (default: $GT_VAULT, then config)")
    ap.add_argument("--plugin-root", help="plugin source (default: read from settings.json)")
    ap.add_argument("--only", action="append", choices=CHECKS,
                    help="run only this check (repeatable)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--fix", action="store_true",
                    help="apply only repairs that cannot lose work (re-wire hooks)")
    a = ap.parse_args(argv)

    wanted = set(a.only or CHECKS)
    root = plugin_root(a.plugin_root)
    vdir = version_dir(root)
    vault = vault_path(a.vault)

    rep = Report()
    if "version" in wanted:
        check_version(rep, root)
    if "components" in wanted:
        check_components(rep, vdir)
    if "wiring" in wanted:
        check_wiring(rep, vdir)
    if "vault" in wanted:
        check_vault(rep, vault)
    if "workers" in wanted:
        check_workers(rep)
    if "push" in wanted:
        check_push(rep)
    if "gt-src" in wanted:
        check_gt_src(rep)
    if "lint" in wanted:
        check_lint(rep, vault)

    if a.fix:
        fix_wiring(rep, vdir, vault)

    if a.json:
        print(json.dumps({"version_dir": str(vdir) if vdir else None,
                          "vault": str(vault) if vault else None,
                          "worst": rep.worst, "checks": rep.rows}, indent=2))
    else:
        print("GOLDEN THREAD doctor — release %s, vault %s"
              % (vdir.name if vdir else "UNKNOWN", vault or "UNKNOWN"))
        for r in rep.rows:
            print("%s %-11s %s" % (MARK[r["state"]], r["check"], r["summary"]))
            # Skip a detail line that merely repeats the summary: several of the
            # underlying scripts lead with the same sentence they end with.
            for line in (r["detail"] or "").splitlines():
                if line.strip() and line.strip() != r["summary"].strip():
                    print("        %s" % line)
            if r["fix"] and r["state"] != OK:
                print("        fix: %s" % r["fix"])
        print("\n%s" % {OK: "all clear",
                        WARN: "needs attention",
                        FAIL: "broken",
                        UNKNOWN: "a check could not run — that is not the same as clean"
                        }[rep.worst])
    return {OK: 0, WARN: 1, FAIL: 1, UNKNOWN: 2}[rep.worst]


def fix_wiring(rep, vdir, vault):
    """The only repair offered: re-register hooks. It adds entries and removes none,
    so nothing a user wrote can be lost. Every other finding needs a human."""
    row = next((r for r in rep.rows if r["check"] == "wiring"), None)
    if not row or row["state"] == OK or not vdir or not vault:
        return
    rc, out = run([sys.executable, str(vdir / "scripts" / "vault_init.py"),
                   "install-core-rules", "--vault", str(vault)])
    row["detail"] = (row["detail"] + "\n--fix: " +
                     ("re-wired" if rc == 0 else "could not re-wire: " + out[-200:]))
    if rc == 0:
        row["state"] = OK
        row["summary"] += " (re-wired by --fix)"


if __name__ == "__main__":
    raise SystemExit(main())
