#!/usr/bin/env python3
"""validate-install.py -- after copygt.sh and install.sh: what is there, what is not, what worked,
what did not.

    validate-install.py --dest <repo> [--src <gt-src>] [--vault V] [--steps FILE]
                        [--report FILE] [--doctor PATH]

Lives ONLY in gt-src, beside copygt.sh, which runs it (dev/sync-gt-src.sh writes both at every
publish; SHA256SUMS covers both). It is never copied into the destination repository.

It does not build a second test suite. The release already ships one: `gt_doctor.py post-install`,
the gate install.sh runs at its end. This runs that gate at its `final` stage and adds what the
gate does not answer -- which plugins the tree DECLARES for this machine and whether each one,
with every skill it ships, actually landed -- and folds in copygt.sh's own steps (--steps).

The report has four sections and one verdict:

  PRESENT   plugins and versions installed, their skills, the hooks wired, the Core rules
  MISSING   anything declared but not installed
  WORKED    each check that ran and passed
  FAILED    each check that failed or COULD NOT RUN -- "could not run" is never a pass
  clean: N/N   passed checks over DECLARED checks, never over what happened to be installed

What is declared:
  * every copygt.sh step in --steps;
  * every plugin in the tree that install.sh installs on this machine -- each non-module plugin,
    and each module whose state is on (asked of the tree's own gt_components); two checks each,
    "installed and enabled at the tree's version" and "every skill present";
  * every row the tree's gt_doctor.py can emit in post-install, read from that file's source.
    A declared row the gate did not produce is FAILED "could not run".

Exit 0 when clean, 1 when not, 2 on bad usage. Stdlib only.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

MARKET = "golden-thread-plugin"
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
GATE_PASS = ("PASS", "INFO", "WARN")          # WARN = e.g. a waiting write queue: reported, not a fail
GATE_TIMEOUT = 300


def vkey(v):
    return tuple(int(x) for x in v.split("."))


def read_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Result:
    def __init__(self):
        self.checks = []          # {"check", "state": worked|failed|could-not-run, "detail"}
        self.present = []
        self.missing = []

    def add(self, check, state, detail=""):
        self.checks.append({"check": check, "state": state, "detail": detail})

    def ok(self, check, detail=""):
        self.add(check, "worked", detail)

    def fail(self, check, detail=""):
        self.add(check, "failed", detail)

    def cannot(self, check, detail=""):
        self.add(check, "could-not-run", detail)

    @property
    def passed(self):
        return sum(1 for c in self.checks if c["state"] == "worked")

    @property
    def clean(self):
        return bool(self.checks) and self.passed == len(self.checks)


def discover(root):
    """[(dir, version, name, version_dir)] -- the rule install.sh uses: newest N.N.N per top-level
    dir that holds .claude-plugin/plugin.json."""
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        vers = [v.name for v in d.iterdir() if v.is_dir() and VERSION.match(v.name)
                and (v / ".claude-plugin" / "plugin.json").is_file()]
        if not vers:
            continue
        ver = max(vers, key=vkey)
        meta = read_json(d / ver / ".claude-plugin" / "plugin.json") or {}
        out.append((d.name, ver, meta.get("name") or d.name, d / ver))
    return out


def module_states(root, gt_rel, home):
    """{module plugin name: state} from the tree's own gt_components, or raise."""
    path = gt_rel / "scripts" / "gt_components.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("gt_components_validate", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    det = mod.module_detail(str(root), str(home), gt_version=gt_rel.name)
    return {d.get("plugin"): d.get("state") for d in det.values()}


def skills_of(vdir):
    base = vdir / "skills"
    if not base.is_dir():
        return []
    return sorted(p.parent.name for p in base.glob("*/SKILL.md"))


def declared_gate_rows(doctor_src):
    """Every row name the release's gt_doctor.py post-install can emit, read from its source."""
    text = doctor_src.read_text(encoding="utf-8", errors="replace")
    names = []
    for pat in (r'gate\.(?:add|needs_upgrade)\(\s*"([a-z0-9-]+)"',
                r'_fold\(\s*gate\s*,\s*\w+\s*,\s*"([a-z0-9-]+)"',
                r'\(\s*"(smoke-[a-z0-9-]+)"\s*,\s*\w+\s*,'):
        for n in re.findall(pat, text):
            if n not in names and n != "post-install":     # post-install = the crash row
                names.append(n)
    return names


def check_plugins(res, root, home, installed, settings):
    plugins = discover(root)
    gt = next((p for p in plugins if p[2] == "gt"), None)
    if not plugins or gt is None:
        res.cannot("plugins declared by the tree", "no gt release under %s" % root)
        return None
    states, why = None, ""
    if any((p[3] / "module.json").is_file() for p in plugins):
        try:
            states = module_states(root, gt[3], home)
        except Exception as exc:          # fail closed: the module plugins become could-not-run
            why = "%s: %s" % (exc.__class__.__name__, str(exc)[:160])
    record = (installed or {}).get("plugins") if isinstance(installed, dict) else None
    enabled = (settings or {}).get("enabledPlugins") or {}
    for d, ver, name, vdir in plugins:
        is_module = (vdir / "module.json").is_file()
        if is_module:
            if states is None:
                res.cannot("plugin %s %s" % (name, ver), "module state unreadable: %s" % why)
                continue
            if states.get(name) != "on":
                res.present.append("%s %s: module off on this machine (not declared)" % (name, ver))
                continue
        key = "%s@%s" % (name, MARKET)
        label = "plugin %s %s installed and enabled" % (name, ver)
        if record is None:
            res.cannot(label, "installed_plugins.json is missing or unreadable")
            res.cannot("plugin %s skills" % name, "nothing to compare against")
            res.missing.append("%s %s (no installed_plugins.json)" % (name, ver))
            continue
        entry = (record.get(key) or [None])[0]
        have = entry.get("version") if isinstance(entry, dict) else None
        path = Path(str(entry.get("installPath") or "")) if isinstance(entry, dict) else None
        if have != ver or path is None or not path.is_dir():
            res.fail(label, "installed: %s" % (have or "NOT INSTALLED")
                     + ("" if have != ver else " (installPath missing)"))
            res.missing.append("%s %s%s" % (name, ver, "" if not have else " (have %s)" % have))
            res.cannot("plugin %s skills" % name, "the plugin is not installed")
            continue
        if not enabled.get(key):
            res.fail(label, "%s is not enabled in settings.json" % key)
            res.missing.append("%s %s enabled in settings.json" % (name, ver))
        else:
            res.ok(label, str(path))
        want = skills_of(vdir)
        got = set(skills_of(path))
        lost = [s for s in want if s not in got]
        if lost:
            res.fail("plugin %s skills" % name, "%d of %d missing: %s"
                     % (len(lost), len(want), ", ".join(lost)))
            res.missing.extend("%s skill %s" % (name, s) for s in lost)
        else:
            res.ok("plugin %s skills" % name, "%d skill(s)" % len(want))
        res.present.append("%s %s, %d skill(s)%s" % (name, have, len(want) - len(lost),
                                                     "" if enabled.get(key) else ", NOT enabled"))
    return gt


def run_gate(res, gt, root, home, vault, doctor):
    if gt is None:
        res.cannot("post-install gate", "no gt release in the tree, so no gate to run")
        return
    try:
        rows = declared_gate_rows(gt[3] / "scripts" / "gt_doctor.py")
    except OSError as exc:
        rows = []
        res.cannot("post-install gate: declared rows", "gt_doctor.py unreadable: %s" % exc)
    if not rows:
        res.cannot("post-install gate: declared rows",
                   "the release's gt_doctor.py declares no post-install rows")
        return
    doctor = Path(doctor) if doctor else home / ".claude" / "golden-thread" / "hooks" / "gt_doctor.py"
    cmd = [sys.executable, str(doctor), "post-install", "--json", "--release", str(gt[3]),
           "--plugin-root", str(root)] + (["--vault", str(vault)] if vault else [])
    data, why = None, ""
    if not doctor.is_file():
        why = "%s is not installed" % doctor
    else:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=GATE_TIMEOUT)
            if p.returncode not in (0, 1):
                why = "gt_doctor.py exited %d: %s" % (p.returncode, (p.stderr or p.stdout)[-200:].strip())
            else:
                data = json.loads(p.stdout)
        except subprocess.TimeoutExpired:
            why = "gt_doctor.py timed out after %ds" % GATE_TIMEOUT
        except ValueError:
            why = "gt_doctor.py printed no readable JSON"
        except OSError as exc:
            why = str(exc)
    got = {}
    for r in (data or {}).get("rows") or []:
        if isinstance(r, dict) and r.get("row"):
            got.setdefault(r["row"], r)
    crash = got.get("post-install")
    for name in rows:
        label = "gate %s" % name
        r = got.get(name)
        if data is None:
            res.cannot(label, why)
        elif r is None:
            res.cannot(label, "the gate produced no %s row%s" % (
                name, ("; " + crash.get("summary", "")) if crash else ""))
        elif r.get("state") in GATE_PASS:
            res.ok(label, "%s: %s" % (r["state"], r.get("summary", "")))
        elif str(r.get("summary", "")).startswith("could not run"):
            res.cannot(label, r.get("summary", ""))
        else:
            res.fail(label, "%s: %s%s" % (r.get("state"), r.get("summary", ""),
                                          (" -- fix: " + r["fix"]) if r.get("fix") else ""))
    for name, r in got.items():
        if name not in rows:          # undeclared rows (e.g. the crash row) can only fail it
            if r.get("state") in GATE_PASS:
                continue
            res.fail("gate %s (undeclared)" % name, "%s: %s" % (r.get("state"), r.get("summary", "")))
    for name, what in (("wiring", "hooks"), ("core-rules", "Core rules"), ("vault", "vault")):
        r = got.get(name)
        if r and r.get("state") in GATE_PASS:
            res.present.append("%s: %s" % (what, r.get("summary", "")))
    if data is not None:
        res.present.append("post-install gate: release %s, %s" % (
            data.get("release"), ", ".join("%d %s" % (v, k) for k, v in sorted(
                (data.get("counts") or {}).items()))))


def render(res, header):
    lines = [header, ""]
    lines.append("PRESENT")
    lines += ["  " + p for p in res.present] or ["  (nothing)"]
    lines.append("MISSING")
    lines += ["  " + m for m in res.missing] or ["  (nothing)"]
    lines.append("WORKED")
    w = [c for c in res.checks if c["state"] == "worked"]
    lines += ["  ok    %s%s" % (c["check"], " -- " + c["detail"] if c["detail"] else "") for c in w] \
        or ["  (nothing)"]
    lines.append("FAILED")
    f = [c for c in res.checks if c["state"] != "worked"]
    lines += ["  %-13s %s%s" % ("COULD NOT RUN" if c["state"] == "could-not-run" else "FAIL",
                                c["check"], " -- " + c["detail"] if c["detail"] else "")
              for c in f] or ["  (nothing)"]
    lines += ["", "clean: %d/%d" % (res.passed, len(res.checks))]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate an install made from gt-src.")
    ap.add_argument("--dest", required=True, help="the repository install.sh ran from")
    ap.add_argument("--src", help="gt-src (for SOURCE.json: plugin path and commit)")
    ap.add_argument("--vault", help="vault for the gate (default: vault-config.json)")
    ap.add_argument("--steps", help="JSON list of copygt.sh steps: {check, ok, detail}")
    ap.add_argument("--report", help="write the report here (and a .json beside it)")
    ap.add_argument("--doctor", help="gt_doctor.py to run (default: the installed hooks copy)")
    a = ap.parse_args(argv)
    home = Path(os.environ.get("HOME") or Path.home())
    dest = Path(a.dest).expanduser().resolve()
    source = read_json(Path(a.src) / "SOURCE.json") if a.src else None
    ppath = (source or {}).get("plugin_path", "").strip("/")
    if not ppath and (dest / "golden-thread-plugin").is_dir():
        ppath = "golden-thread-plugin"
    root = dest / ppath if ppath else dest
    res = Result()
    if a.steps:
        steps = read_json(a.steps)
        if not isinstance(steps, list):
            res.cannot("copygt.sh steps", "%s is unreadable" % a.steps)
        for s in steps or []:
            (res.ok if s.get("ok") else res.fail)(s.get("check", "?"), s.get("detail", ""))
    installed = read_json(home / ".claude" / "plugins" / "installed_plugins.json")
    settings = read_json(home / ".claude" / "settings.json")
    try:
        gt = check_plugins(res, root, home, installed, settings)
    except Exception as exc:
        gt = None
        res.cannot("plugins declared by the tree", "%s: %s" % (exc.__class__.__name__, exc))
    try:
        run_gate(res, gt, root, home, a.vault, a.doctor)
    except Exception as exc:
        res.cannot("post-install gate", "%s: %s" % (exc.__class__.__name__, exc))
    header = "GOLDEN THREAD install validation — %s — %s%s" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), root,
        (" — gt-src %s" % source.get("commit", "?")[:12]) if source else "")
    text = render(res, header)
    sys.stdout.write(text)
    if a.report:
        rp = Path(a.report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(text, encoding="utf-8")
        rp.with_suffix(".json").write_text(json.dumps({
            "clean": res.clean, "passed": res.passed, "declared": len(res.checks),
            "present": res.present, "missing": res.missing, "checks": res.checks,
            "source": source}, indent=1) + "\n", encoding="utf-8")
    return 0 if res.clean else 1


if __name__ == "__main__":
    sys.exit(main())
