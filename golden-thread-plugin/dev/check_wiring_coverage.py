#!/usr/bin/env python3
"""Every shipped item must reach its destination. Proven by installing, not by reading.

    dev/check_wiring_coverage.py <version-dir> [--keep]

Exit 0 = every hook, script, skill, template, tool and Core rule that ships was found
where it has to be after a real install. Exit 1 = at least one did not.

## Why this exists, in the words of the failure

0.12.0 shipped `guard_vault_writes.sh`. It was copied to the hooks directory, listed in
MANIFEST.json, and verified by the component check -- which reported "clean". It was
never added to `settings.json`, so it never ran. Three releases in a row reported a
healthy install while a Core-rule mechanism sat inert:

  0.12.0  install.sh wired only its own hooks; the enforcement hooks were wired by
          vault_init, which ran only when a vault was CREATED, never on an upgrade.
  0.12.1  the wiring call was added -- inside the block gated on the vault being a
          GIT REPO. A vault that was not a repo stayed inert, and was reported as no
          vault at all.
  0.12.2  fixed on the build machine's own vault, which is a git repo, so the author
          confirmed the fix from the one vantage point where it already worked.

Every one of those was invisible to a check that reads files or asks whether a list
matches a list. The only thing that catches them is DOING the install and then asking
each shipped item where it ended up.

## What it asserts, derived rather than listed

  hooks/*.sh        -> declared in HOOK_REGISTRATIONS, AND present in settings.json
                       with a command path that exists
  HOOK_DIR_SCRIPTS  -> copied into ~/.claude/golden-thread/hooks/
  skills/*/SKILL.md -> present in the installed plugin cache
  templates/tools/  -> seeded into <vault>/Projects/golden-thread/tools/
  core-rules/*.md   -> installed into the vault's core-rules/
  scripts/*.py      -> present in the cache (what the skills invoke)
  module hooks      -> (0.14.0) an ON module's declared hooks wired and its hookdir_scripts
                       in the hooks dir; an OFF module's hooks NOT wired and its hook-dir
                       files absent. On/off is the module's effective state in the
                       sandbox home, read by the release's own gt_components.
  module matrix     -> (0.15.0) the same module checks, plus every ON module's skills and
                       scripts in ITS plugin cache and every OFF module's cache gone, run
                       three times: the defaults, then `--with` every module (all on),
                       then `--without` every module (all off). watch and report card are
                       the first modules with hooks, and farm defaults off, so the default
                       install alone would never exercise an off module that had hooks or
                       an on module that defaults off.

Nothing here is a hand-maintained list of expected files: each set is read from the
release being tested, so a new file is covered the moment it ships. A file that ships
and belongs nowhere is itself the finding -- that is the shape of the 0.12.0 bug.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plugins  # noqa: E402  the one plugin-discovery rule, shared with the shell gates

PY = os.environ.get("GT_PYTHON", sys.executable or "python3")

# A shipped hook script that is deliberately not registered on its own: it is called
# BY another hook rather than by the harness. Each exemption is a decision on the
# record, and the check fails if one of these stops existing.
NOT_REGISTERED = {
    "guard_session_claims.py": "called by guard_session_claims.sh, not by the harness",
    "guard_vault_writes.py": "called by guard_vault_writes.sh, not by the harness",
    "guard_test_before_commit.py": "called by guard_test_before_commit.sh, not by the harness",
    "guard_protected_paths.py": "called by guard_protected_paths.sh, not by the harness",
    "gt_paths.py": "a library the hooks import; it is not a hook",
}


def load_components(version_dir):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gt_components_cov", str(Path(version_dir) / "scripts" / "gt_components.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def settings_commands(home):
    """Every command string wired in settings.json, with the event it is wired to."""
    f = Path(home) / ".claude" / "settings.json"
    if not f.is_file():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for event, blocks in (data.get("hooks") or {}).items():
        for b in blocks if isinstance(blocks, list) else []:
            for h in (b.get("hooks") or []) if isinstance(b, dict) else []:
                out.append((event, h.get("command") or ""))
    return out


def strip_enforcement(home, comp):
    """Remove the enforcement entries from settings.json -> the names removed.

    Reproduces the state every existing machine was in when a new enforcement hook
    shipped: the vault configured, the plugin installed, the entry simply absent.
    """
    names = [r["script"] for r in comp.HOOK_REGISTRATIONS
             if r.get("owner", "").startswith("vault_init.py")]
    f = Path(home) / ".claude" / "settings.json"
    if not f.is_file():
        return names
    data = json.loads(f.read_text(encoding="utf-8"))
    for blocks in (data.get("hooks") or {}).values():
        for b in blocks if isinstance(blocks, list) else []:
            if isinstance(b, dict):
                b["hooks"] = [h for h in b.get("hooks", [])
                              if not any(n in (h.get("command") or "") for n in names)]
    f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return names


def module_findings(repo, home, comp, gt_version=None):
    """Problems with module hooks after an install into `home` from plugin root `repo`.

    ON  -> every declared hook wired under its event; every hookdir_scripts file and
           hook script present in the hooks dir; every file in the module's hooks/ is
           declared (a .py beside a declared .sh of the same stem is its helper).
    OFF -> none of its hooks wired and none of its hook-dir files installed -- "off"
           means absent, and a leftover registration runs a module the user declined.
    A release without the module reader (before 0.14.0) has no modules: [].
    """
    if not hasattr(comp, "module_detail"):
        return []
    problems = []
    try:
        det = comp.module_detail(str(repo), str(home), gt_version=gt_version)
    except Exception as exc:
        return ["module states could not be read: %s" % exc]
    wired = settings_commands(home)
    hooks_dir = Path(home) / ".claude" / "golden-thread" / "hooks"
    gt_names = set(getattr(comp, "HOOK_DIR_SCRIPTS", ())) | {
        r["script"] for r in comp.HOOK_REGISTRATIONS}
    for m in comp.discover_modules(str(repo)):
        name, data, vd = m["name"], m["data"], Path(m["version_dir"])
        if m["reasons"]:
            problems.append("module %s has an invalid module.json: %s"
                            % (name, "; ".join(m["reasons"])))
            continue
        state = det.get(name, {}).get("state")
        hooks = data.get("hooks") or []
        files = {h["script"] for h in hooks} | set(data.get("hookdir_scripts") or [])
        if state == "on":
            for h in hooks:
                if not any(e == h["event"] and h["script"] in c for e, c in wired):
                    problems.append("module %s: hook %s is declared for %s but NOT wired in "
                                    "settings.json after a full install — it ships inert"
                                    % (name, h["script"], h["event"]))
            for fname in sorted(files):
                if not (hooks_dir / fname).is_file():
                    problems.append("module %s: %s was not installed to "
                                    "~/.claude/golden-thread/hooks/" % (name, fname))
            declared = {h["script"] for h in hooks}
            stems = {Path(s).stem for s in declared if s.endswith(".sh")}
            for fp in sorted((vd / "hooks").glob("*")) if (vd / "hooks").is_dir() else []:
                if not fp.is_file() or fp.name.startswith("."):
                    continue
                if fp.name in declared or (fp.suffix == ".py" and fp.stem in stems):
                    continue
                problems.append("module %s: hooks/%s ships but is declared in NO hook of its "
                                "module.json — it can never run" % (name, fp.name))
        else:
            for h in hooks:
                if h["script"] in gt_names:
                    continue
                if any(h["script"] in c for _e, c in wired):
                    problems.append("module %s is OFF but its hook %s is still wired in "
                                    "settings.json" % (name, h["script"]))
            for fname in sorted(files - gt_names):
                if (hooks_dir / fname).exists():
                    problems.append("module %s is OFF but %s is still in "
                                    "~/.claude/golden-thread/hooks/" % (name, fname))
    return problems


def module_cache_findings(repo, home, comp, gt_version=None):
    """ON module -> its skills and scripts are in its plugin cache; OFF -> no cache at all."""
    if not hasattr(comp, "module_detail"):
        return []
    try:
        det = comp.module_detail(str(repo), str(home), gt_version=gt_version)
    except Exception as exc:
        return ["module states could not be read: %s" % exc]
    cache_root = Path(home) / ".claude" / "plugins" / "cache" / "golden-thread-plugin"
    problems = []
    for m in comp.discover_modules(str(repo)):
        if m["reasons"]:
            continue
        data, vd = m["data"], Path(m["version_dir"])
        cache = cache_root / data["plugin"]
        if det.get(m["name"], {}).get("state") == "on":
            for sk in data.get("skills") or []:
                if not (cache / vd.name / "skills" / sk / "SKILL.md").is_file():
                    problems.append("module %s: skill %s is not in the installed cache"
                                    % (m["name"], sk))
            for sc in data.get("scripts") or []:
                if not (cache / vd.name / "scripts" / sc).is_file():
                    problems.append("module %s: scripts/%s is not in the installed cache"
                                    % (m["name"], sc))
        elif cache.exists():
            problems.append("module %s is OFF but its plugin cache %s is still there"
                            % (m["name"], cache))
    return problems


def run_upgrade(repo, home, *extra):
    """install.sh against a vault that already exists (with `extra` arguments, if any)."""
    env = dict(os.environ, HOME=str(home))
    env.pop("GT_VAULT", None)
    for var in [k for k in env if k.startswith("CLAUDE")]:
        env.pop(var, None)
    # --force-manifest-mismatch, deliberately: this checker's whole method is to install
    # a TAMPERED tree (an orphan hook added, a shipped script broken) and then ask where
    # each file ended up. Since 0.12.7 install.sh refuses a source whose executing files
    # disagree with its manifest, which would stop every negative case here at exit 6
    # before the thing under test could be observed. This is the one caller for which the
    # override is the correct behaviour rather than an escape hatch.
    p = subprocess.run(["bash", str(Path(repo) / "install.sh"), "--force-manifest-mismatch",
                        *extra],
                       capture_output=True, text=True, timeout=600, env=env,
                       stdin=subprocess.DEVNULL)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def run_install(repo, home, vault):
    env = dict(os.environ, HOME=str(home))
    env.pop("GT_VAULT", None)
    for var in [k for k in env if k.startswith("CLAUDE")]:
        env.pop(var, None)
    p = subprocess.run(["bash", str(Path(repo) / "install.sh"), "--vault", str(vault),
                        "--force-manifest-mismatch"],
                       capture_output=True, text=True, timeout=600, env=env,
                       stdin=subprocess.DEVNULL)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def check(version_dir, keep=False, module_matrix=True):
    version_dir = Path(version_dir).resolve()
    plugin_root = version_dir.parent.parent
    comp = load_components(version_dir)
    problems = []

    sandbox = Path(tempfile.mkdtemp(prefix="gt-wiring-"))
    try:
        home = sandbox / "home"
        home.mkdir()
        vault = sandbox / "vault"
        repo = sandbox / "repo"
        repo.mkdir()
        shutil.copy2(plugin_root / "install.sh", repo / "install.sh")
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
        shutil.copytree(version_dir, repo / "golden-thread" / version_dir.name, ignore=ignore)
        # Every OTHER plugin the repo ships rides along at its newest version, found by
        # the one discovery rule (dev/plugins.py) rather than a gt-wiki named here.
        for name, newest, _ in plugins.discover(plugin_root):
            if name != version_dir.parent.name:
                shutil.copytree(newest, repo / name / newest.name, ignore=ignore)

        # PHASE 1 — a first install that names its vault.
        rc, out = run_install(repo, home, vault)
        if rc != 0:
            problems.append("install.sh exited %s with a vault given:\n%s" % (rc, out[-800:]))
            return problems

        # PHASE 2 — the UPGRADE, which is the path that actually broke three times.
        #
        # Phase 1 alone proves nothing about it: `--vault` creates the vault, and
        # creating a vault has always wired the enforcement hooks. The bug lived in the
        # other path — a vault that already exists, a plain `install.sh`, a hook newly
        # shipped by this release. So: strip the enforcement entries, leave the vault
        # configured, and run the installer the way an upgrading user does.
        stripped = strip_enforcement(home, comp)
        rc2, out2 = run_upgrade(repo, home)
        if rc2 != 0:
            problems.append("install.sh exited %s upgrading over an existing vault:\n%s"
                            % (rc2, out2[-800:]))
        for name in stripped:
            if not any(name in c for _, c in settings_commands(home)):
                problems.append(
                    "hooks/%s was NOT re-wired by an upgrade over an existing vault — "
                    "it ships inert for everyone who already had a vault (the "
                    "0.12.0/0.12.1 bug)" % name)

        wired = settings_commands(home)
        wired_names = {os.path.basename(c.split()[-1] if " " in c else c): (e, c)
                       for e, c in wired}
        # A command can be `python3 /path/x.py check ...`; match on any token.
        def is_wired(name):
            return any(name in c for _, c in wired)

        # ---- 1. every shipped hook script ------------------------------------
        declared = {r["script"] for r in comp.HOOK_REGISTRATIONS}
        for f in sorted((version_dir / "hooks").glob("*")):
            if not f.is_file() or f.name.startswith("."):
                continue
            if f.name in NOT_REGISTERED:
                continue
            if f.name not in declared:
                problems.append(
                    "hooks/%s ships but is declared in NO hook registration — it can "
                    "never run. (If it is a helper, name it in NOT_REGISTERED with the "
                    "reason.)" % f.name)
                continue
            if not is_wired(f.name):
                problems.append("hooks/%s is declared but NOT wired in settings.json "
                                "after a full install — it ships inert" % f.name)

        # ---- 2. every declared registration points at a real file ------------
        for event, cmd in wired:
            if "golden-thread" not in cmd:
                continue
            path = None
            for tok in cmd.split():
                tok = tok.strip('"')
                if tok.endswith((".sh", ".py")):
                    path = tok
                    break
            if path and not Path(path).exists():
                problems.append("settings.json wires %s -> %s, which does not exist"
                                % (event, path))

        # ---- 3. scripts that must live in the hooks directory ----------------
        hooks_dir = home / ".claude" / "golden-thread" / "hooks"
        for name in getattr(comp, "HOOK_DIR_SCRIPTS", ()):
            if not (hooks_dir / name).is_file():
                problems.append("%s is in HOOK_DIR_SCRIPTS but was not installed to "
                                "~/.claude/golden-thread/hooks/" % name)

        # ---- 4. every skill reached the cache --------------------------------
        cache = home / ".claude" / "plugins" / "cache" / "golden-thread-plugin" / "gt" / version_dir.name
        for sk in sorted((version_dir / "skills").iterdir()):
            if not (sk / "SKILL.md").is_file():
                continue
            if not (cache / "skills" / sk.name / "SKILL.md").is_file():
                problems.append("skill %s ships but is not in the installed cache" % sk.name)

        # ---- 5. every script reached the cache -------------------------------
        for s in sorted((version_dir / "scripts").glob("*.py")):
            if not (cache / "scripts" / s.name).is_file():
                problems.append("scripts/%s ships but is not in the installed cache" % s.name)

        # ---- 6. every vault tool was seeded ----------------------------------
        tools = vault / "Projects" / "golden-thread" / "tools"
        for t in sorted((version_dir / "templates" / "tools").glob("*.py")):
            if not (tools / t.name).is_file():
                problems.append("templates/tools/%s ships but was not seeded into the "
                                "vault" % t.name)

        # ---- 7. every Core rule reached the vault ----------------------------
        rules_src = version_dir / "templates" / "core-rules"
        rules_dst = vault / "Projects" / "golden-thread" / "core-rules"
        for r in sorted(rules_src.glob("*.md")):
            if not (rules_dst / r.name).is_file():
                problems.append("core-rules/%s ships but was not installed into the "
                                "vault" % r.name)

        # ---- 8. a rule claiming enforcement must have its mechanism wired -----
        for r in sorted(rules_src.glob("core_*.md")):
            text = r.read_text(encoding="utf-8", errors="replace")
            if "enforcement: validated" not in text:
                continue
            if not any(e in ("PreToolUse", "Stop", "UserPromptSubmit") for e, _ in wired):
                problems.append("%s declares enforcement: validated but no enforcement "
                                "event is wired at all" % r.name)
                break

        # ---- 9. module hooks: wired when on, absent when off ------------------
        problems.extend(module_findings(repo, home, comp, gt_version=version_dir.name))
        problems.extend(module_cache_findings(repo, home, comp, gt_version=version_dir.name))

        # ---- 10. the module matrix: every module on, then every module off -----
        names = [m["name"] for m in comp.discover_modules(str(repo))] \
            if hasattr(comp, "discover_modules") else []
        # `--no-module-matrix` exists for fixture tests that break ONE non-module thing on
        # purpose: two extra installs per fixture pushed them past the harness timeout
        # (0.15.0 gate). The release gate itself always runs the matrix.
        for label, flag in (("every module on", "--with"), ("every module off", "--without")):
            if not names or not module_matrix:
                break
            args = [a for n in names for a in (flag, n)]
            rc3, out3 = run_upgrade(repo, home, *args)
            if rc3 != 0:
                problems.append("install.sh exited %s with %s:\n%s" % (rc3, label, out3[-800:]))
                continue
            for finding in (module_findings(repo, home, comp, gt_version=version_dir.name)
                            + module_cache_findings(repo, home, comp,
                                                    gt_version=version_dir.name)):
                problems.append("[%s] %s" % (label, finding))
    finally:
        if keep:
            print("sandbox kept at %s" % sandbox, file=sys.stderr)
        else:
            shutil.rmtree(sandbox, ignore_errors=True)
    # Two shipped files installing to ONE destination: the manifest then records two
    # hashes for one path, so the drift check must disagree with one of them forever.
    # Found 2026-09-12 -- gt_paths.py shipped from both hooks/ (a 0.12.2-era copy) and
    # scripts/ (current). install.sh copied hooks/* first and overwrote with scripts/,
    # so the right file won by ordering while every machine reported permanent drift.
    for dst, rels in sorted(comp.duplicate_destinations(version_dir).items()):
        problems.append("%s is installed from %d shipped files (%s) -- the manifest keeps "
                        "a hash for each, so drift can never read clean. Ship it once."
                        % (dst, len(rels), ", ".join(rels)))

    return problems


def main(argv):
    keep = "--keep" in argv
    module_matrix = "--no-module-matrix" not in argv
    argv = [a for a in argv if a not in ("--keep", "--no-module-matrix")]
    if not argv:
        print("usage: check_wiring_coverage.py <version-dir> [--keep] [--no-module-matrix]", file=sys.stderr)
        return 2
    problems = check(argv[0], keep=keep, module_matrix=module_matrix)
    if problems:
        for p in problems:
            print("  " + p)
        print("wiring coverage: %d problem(s)" % len(problems))
        return 1
    print("wiring coverage: every shipped hook, script, skill, tool and Core rule "
          "reached its destination in a real install")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
