"""install.sh: installs gt + gt-wiki into HOME and wires the reporting hooks.

install.sh writes MANIFEST.json into the version dir it installs FROM, so every
run here uses a throwaway copy of the repo (install.sh + the newest version dirs)
and a sandbox HOME. Nothing touches the real repo or the real ~/.claude.

Contracts pinned here:
  * gt and gt-wiki land in ~/.claude/plugins/cache/... AND the marketplace, file for
    file; both are registered and enabled;
  * the five hooks install.sh owns for gt are registered in settings.json and verified,
    and point at files that exist in the sandbox; since 0.15.0 the watch and report-card
    modules wire the other four (gt_watch.py, gt_report_card.py x3) at the same paths with
    the same commands, tagged with their module;
  * the demo is module `demo` (plugin gt-demo) since 0.14.0: gt itself ships no demo, and
    install_demo=no in vault-config.json (recorded as the module choice by the machine
    migration) leaves gt-demo out of cache, marketplace and settings -- including a demo
    left by an earlier install -- while gt and gt-wiki are installed file for file;
  * a second run is idempotent; foreign settings survive; old gt caches are pruned;
  * a bad version request or a manifest/dir mismatch refuses before installing.
"""
import json
import shutil
import unittest
from pathlib import Path

from _harness import Sandbox, REPO, GT, WIKI, WATCH, REPORT_CARD, latest_version_dir

INSTALL = REPO / "install.sh"
DEMO = ("skills/gt-demo", "scripts/gt_demo.sh", "templates/demo-pizzabot")
GT_DIRS = (".claude-plugin", "skills", "scripts", "templates", "commands", "hooks")
WIKI_DIRS = (".claude-plugin", "skills", "scripts", "templates", "commands")
# 0.12.9 added the protected-path guard. 0.15.0 moved gt_watch.py and gt_report_card.py
# into the watch and report-card modules (MODULE_OWNED): gt alone now wires five.
OWNED = {"SessionStart": {"gt_components.py", "gt_workers.py", "gt_version_check.py",
                          "gt_push_check.py"},
         "PreToolUse": {"guard_protected_paths.sh"}}
MODULE_OWNED = {("SessionStart", "gt_watch.py"): ("watch", ["--hook"]),
                ("SessionStart", "gt_report_card.py"): ("report-card", ["surface", "--hook"]),
                ("PreCompact", "gt_report_card.py"): ("report-card", []),
                ("SessionEnd", "gt_report_card.py"): ("report-card", [])}
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
try:
    DEMO_MODULE = latest_version_dir(REPO / "golden-thread-demo")
except (RuntimeError, OSError):
    DEMO_MODULE = None


def files_under(root: Path, dirs):
    out = set()
    for d in dirs:
        base = root / d
        if base.is_dir():
            out |= {str(p.relative_to(root)) for p in base.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts}
    return out


def is_demo(rel):
    return any(rel == d or rel.startswith(d + "/") for d in DEMO)


class InstallTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo_copy(self.tmp / "src" / "golden-thread-plugin")

    def make_repo_copy(self, dest: Path) -> Path:
        dest.mkdir(parents=True)
        shutil.copy2(INSTALL, dest / "install.sh")
        shutil.copytree(GT, dest / "golden-thread" / GT.name, ignore=IGNORE)
        shutil.copytree(WIKI, dest / "golden-thread-wiki" / WIKI.name, ignore=IGNORE)
        self.regenerate_manifest(dest / "golden-thread" / GT.name)
        return dest

    def regenerate_manifest(self, version_dir: Path):
        """Make the fixture's manifest describe the fixture.

        Since 0.12.7 install.sh REFUSES a source whose executing files disagree with its
        manifest. This copy is a synthetic tree -- IGNORE drops __pycache__ and friends,
        and more importantly the working tree's manifest is stale for as long as someone
        is mid-edit, which is most of the time during development. Without this, every
        install test fails with a manifest refusal that has nothing to do with what the
        test is checking.

        Tests that want a mismatch create one AFTER this, which is the honest way round:
        the fixture starts consistent and each test breaks exactly what it means to break.
        """
        self.py(version_dir / "scripts" / "gt_components.py", "manifest", version_dir)

    def install(self, *args, repo=None):
        """Install the plugin only.

        --no-vault is passed unless a test says otherwise: since 0.12.2 an install with
        no vault and no flag STOPS with exit 4 rather than finishing with the
        enforcement hooks inert. These tests are about what lands on disk, so they opt
        out of that decision explicitly — VaultIsPartOfTheInstall covers the decision
        itself.
        """
        if not any(a in ("--vault", "--no-vault") or str(a).startswith("--vault=")
                   for a in args):
            args = (*args, "--no-vault")
        return self.sh((repo or self.repo) / "install.sh", *args, timeout=300)

    # -- paths -------------------------------------------------------------------
    @property
    def plugins(self):
        return self.home / ".claude" / "plugins"

    def cache(self, which="gt"):
        ver = GT.name if which == "gt" else WIKI.name
        return self.plugins / "cache" / "golden-thread-plugin" / which / ver

    def market(self, which="gt"):
        return self.plugins / "marketplaces" / "golden-thread-plugin" / "plugins" / which

    def settings(self):
        return json.loads((self.home / ".claude" / "settings.json").read_text())

    def src(self, which="gt"):
        return self.repo / ("golden-thread" if which == "gt" else "golden-thread-wiki") / (
            GT.name if which == "gt" else WIKI.name)

    # ------------------------------------------------------------------------------
    def test_scripts_parse(self):
        for script in (INSTALL, REPO / "package.sh"):
            with self.subTest(script=script.name):
                self.assertOk(self.run_cmd(["bash", "-n", script]))

    def test_fresh_install_lays_down_both_plugins(self):
        p = self.install()
        self.assertOk(p)
        for which, dirs in (("gt", GT_DIRS), ("gt-wiki", WIKI_DIRS)):
            want = files_under(self.src(which), dirs)
            self.assertTrue(want)
            with self.subTest(which=which, where="cache"):
                self.assertEqual(files_under(self.cache(which), dirs), want)
            with self.subTest(which=which, where="marketplace"):
                self.assertEqual(files_under(self.market(which), dirs), want)
        mp = json.loads((self.plugins / "marketplaces" / "golden-thread-plugin" / ".claude-plugin"
                         / "marketplace.json").read_text())
        self.assertEqual({x["name"] for x in mp["plugins"]}, {"gt", "gt-wiki"})
        inst = json.loads((self.plugins / "installed_plugins.json").read_text())["plugins"]
        self.assertEqual(inst["gt@golden-thread-plugin"][0]["installPath"], str(self.cache("gt")))
        self.assertEqual(inst["gt@golden-thread-plugin"][0]["version"], GT.name)
        self.assertEqual(inst["gt-wiki@golden-thread-plugin"][0]["version"], WIKI.name)
        known = json.loads((self.plugins / "known_marketplaces.json").read_text())
        self.assertIn("golden-thread-plugin", known)
        self.assertEqual(self.settings()["enabledPlugins"],
                         {"gt@golden-thread-plugin": True, "gt-wiki@golden-thread-plugin": True})
        # 0.14.0: the demo moved to the gt-demo module; gt carries none of it
        for d in DEMO:
            self.assertFalse((self.cache() / d).exists(), d)
        hooks_dir = self.home / ".claude" / "golden-thread" / "hooks"
        for f in ("inject_core_rules.sh", "validate_response.sh", "gt_paths.py", "gt_components.py"):
            self.assertTrue((hooks_dir / f).is_file(), f)
        self.assertTrue((self.src() / "MANIFEST.json").is_file())

    def registered(self):
        hooks = self.settings().get("hooks", {})
        return {ev: [h["command"] for block in blocks for h in block.get("hooks", [])]
                for ev, blocks in hooks.items()}

    def test_gt_hooks_registered_and_verified(self):
        p = self.install()
        self.assertOk(p)
        self.assertIn("Registered 5 hooks", p.stdout)
        self.assertIn("Verified hook wiring", p.stdout)
        self.assertNotIn("INCOMPLETE", p.stdout)
        reg = self.registered()
        self.assertEqual(sum(len(v) for v in reg.values()), 5, reg)
        hooks_dir = str(self.home / ".claude" / "golden-thread" / "hooks")
        for event, scripts in OWNED.items():
            cmds = reg.get(event, [])
            self.assertEqual(len(cmds), len(scripts), event)
            for s in scripts:
                hit = [c for c in cmds if s in c]
                self.assertEqual(len(hit), 1, f"{event}/{s}: {cmds}")
                self.assertIn(hooks_dir, hit[0])
                self.assertTrue((Path(hooks_dir) / s).is_file(), s)
        # the wiring check, asked independently, agrees
        w = self.run_cmd(["python3", self.src() / "scripts" / "gt_components.py", "wiring",
                          self.src(), "--owner", "install.sh"])
        self.assertOk(w)

    def test_watch_and_report_card_modules_wire_what_gt_used_to(self):
        """0.15.0: the four entries gt wired until 0.14.0 now come from module.json, with
        the command gt used to write -- so an existing install's entries are the module's."""
        if WATCH is None or REPORT_CARD is None:
            self.skipTest("watch / report-card modules not in this tree")
        for mod in (WATCH, REPORT_CARD):
            shutil.copytree(mod, self.repo / mod.parent.name / mod.name, ignore=IGNORE)
        p = self.install()
        self.assertOk(p)
        self.assertIn("Registered 9 hooks", p.stdout)
        self.assertIn("Verified hook wiring", p.stdout)
        self.assertNotIn("gt-report-card skills:", p.stdout, "an empty skills heading")
        hooks_dir = self.home / ".claude" / "golden-thread" / "hooks"
        reg = self.registered()
        for (event, script), (_module, args) in MODULE_OWNED.items():
            hit = [c for c in reg.get(event, []) if script in c]
            want = " ".join(["python3", str(hooks_dir / script)] + args)
            self.assertEqual(hit, [want], "%s/%s: %s" % (event, script, reg.get(event)))
            self.assertTrue((hooks_dir / script).is_file(), script)
        self.assertFalse((self.cache() / "scripts" / "gt_watch.py").exists())
        self.assertFalse((self.cache() / "scripts" / "gt_report_card.py").exists())
        self.assertFalse((self.cache() / "templates" / "watch.md").exists())
        w = self.run_cmd(["python3", self.src() / "scripts" / "gt_components.py", "wiring",
                          self.src(), "--owner", "install.sh"])
        self.assertOk(w)
        self.assertIn("all 9 declared hooks are wired", w.stdout)

    # -- the demo module (0.14.0; until then install_demo=no stripped it out of gt) ----
    def add_demo_module(self):
        if DEMO_MODULE is None:
            self.skipTest("golden-thread-demo module not in this tree")
        shutil.copytree(DEMO_MODULE, self.repo / "golden-thread-demo" / DEMO_MODULE.name,
                        ignore=IGNORE)

    def demo_absent(self):
        key = "gt-demo@golden-thread-plugin"
        self.assertFalse((self.plugins / "cache" / "golden-thread-plugin" / "gt-demo").exists())
        self.assertFalse(self.market("gt-demo").exists())
        self.assertNotIn(key, self.settings()["enabledPlugins"])
        inst = json.loads((self.plugins / "installed_plugins.json").read_text())["plugins"]
        self.assertNotIn(key, inst)

    def test_install_demo_no_omits_exactly_the_demo(self):
        self.add_demo_module()
        self.config(install_demo="no")
        p = self.install()
        self.assertOk(p)
        self.assertIn("demo off (by your choice)", p.stdout)
        self.assertFalse([l for l in p.stdout.splitlines() if "/gt-demo:gt-demo" in l],
                         "summary lists the demo skill as installed")
        self.demo_absent()
        want = files_under(self.src(), GT_DIRS)
        self.assertFalse([f for f in want if is_demo(f)], "gt source still ships the demo")
        for where, root in (("cache", self.cache()), ("marketplace", self.market())):
            with self.subTest(where=where):
                self.assertEqual(files_under(root, GT_DIRS), want)
        # gt-wiki is untouched by the demo setting
        self.assertEqual(files_under(self.cache("gt-wiki"), WIKI_DIRS),
                         files_under(self.src("gt-wiki"), WIKI_DIRS))

    def test_install_demo_no_clears_a_demo_left_by_an_earlier_install(self):
        self.add_demo_module()
        self.assertOk(self.install())
        self.assertTrue((self.market("gt-demo") / "skills" / "gt-demo" / "SKILL.md").is_file())
        self.config(install_demo="no")
        p = self.install()
        self.assertOk(p)
        self.assertIn("removed cache", p.stdout)
        self.demo_absent()

    def test_second_run_is_idempotent(self):
        self.assertOk(self.install())
        s1 = self.settings()
        files1 = files_under(self.cache(), GT_DIRS)
        p = self.install()
        self.assertOk(p)
        self.assertEqual(self.settings(), s1, "second run changed settings.json")
        self.assertEqual(sum(len(v) for v in self.registered().values()), 5, "hooks duplicated")
        self.assertEqual(files_under(self.cache(), GT_DIRS), files1)
        vers = [d.name for d in (self.plugins / "cache" / "golden-thread-plugin" / "gt").iterdir()]
        self.assertEqual(vers, [GT.name])
        self.assertIn("Verified hook wiring", p.stdout)

    def test_foreign_settings_survive_and_old_cache_is_pruned(self):
        user_hook = {"hooks": [{"type": "command", "command": "echo user-hook"}]}
        (self.home / ".claude" / "settings.json").write_text(json.dumps({
            "theme": "dark", "enabledPlugins": {"other@x": True},
            "hooks": {"SessionStart": [user_hook], "Stop": [user_hook]}}))
        old = self.plugins / "cache" / "golden-thread-plugin" / "gt" / "0.0.1"
        old.mkdir(parents=True)
        (old / "stale.txt").write_text("x")
        self.assertOk(self.install())
        s = self.settings()
        self.assertEqual(s["theme"], "dark")
        self.assertTrue(s["enabledPlugins"]["other@x"])
        self.assertIn("echo user-hook", self.registered()["SessionStart"])
        self.assertEqual(self.registered()["Stop"], ["echo user-hook"])
        self.assertEqual(len(self.registered()["SessionStart"]), 5)   # user hook + 4 gt SessionStart hooks (watch/report-card are modules since 0.15.0)
        self.assertFalse(old.exists(), "superseded gt cache left behind")
        backups = list((self.home / ".claude" / "golden-thread" / "backups").glob("settings.json.*"))
        self.assertTrue(backups, "pre-existing settings.json was not backed up")

    def test_path_with_apostrophe_and_space(self):
        repo = self.make_repo_copy(self.tmp / "Sam's Projects" / "golden thread plugin")
        p = self.install(repo=repo)
        self.assertOk(p)
        self.assertIn("Verified hook wiring", p.stdout)

    def test_unknown_pinned_version_refuses_before_installing(self):
        p = self.install("9.9.9")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("9.9.9", p.stdout)
        self.assertFalse((self.home / ".claude" / "settings.json").exists())
        self.assertFalse(self.plugins.exists())

    # -- the source tree is not the installer's to modify --------------------------
    def test_install_leaves_the_source_tree_byte_identical(self):
        """install.sh must not rewrite MANIFEST.json where it installs FROM.

        It used to regenerate it every run, changing only the `generated` timestamp,
        which dirtied git and made dev/sync-gt-src.sh refuse to publish until someone
        committed the noise. Four such commits were made in one day.
        """
        manifest = self.src() / "MANIFEST.json"
        before = manifest.read_bytes() if manifest.is_file() else None
        self.assertIsNotNone(before, "fixture has no MANIFEST.json to protect")
        self.assertOk(self.install())
        self.assertEqual(manifest.read_bytes(), before,
                         "install.sh rewrote MANIFEST.json in the source tree")

    def test_install_writes_a_manifest_only_when_none_exists(self):
        """gt_components.compare() cannot work without one, so absent still means write."""
        manifest = self.src() / "MANIFEST.json"
        manifest.unlink()
        self.assertOk(self.install())
        self.assertTrue(manifest.is_file(),
                        "no manifest was present and install.sh did not create one")

    def test_stale_manifest_in_an_executing_file_now_refuses(self):
        """SUPERSEDED PIN, deliberately inverted.

        This asserted that a stale manifest still installs, to stop a refusal arriving as
        a side effect of the "leave the source tree clean" fix. That was correct while the
        refusal was only a proposal. The owner decided it on 2026-09-12
        (2026-09-11-install-refuse-stale-manifest): refuse for files that EXECUTE, warn for
        files that are copied, exempt a developer's uncommitted edit.

        Kept rather than deleted, with the old intent recorded, so nobody reintroduces the
        old assertion from the reasoning that produced it. ManifestMismatch covers the full
        behaviour; this one pins that the specific file named here is on the refusing side.
        """
        victim = self.src() / "scripts" / "gt_settings.py"
        victim.write_text(victim.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
        p = self.install()
        self.assertEqual(p.returncode, 6,
                         "scripts/ executes, so a stale manifest there must refuse\n" + p.stdout)
        self.assertIn("gt_components.py manifest", p.stdout,
                      "the refusal names the command that fixes it")

    # -- the skill summary is derived, not typed -----------------------------------
    def _summary_skills(self, out, prefix="/gt:"):
        names = set()
        for line in out.splitlines():
            line = line.strip()
            if line.startswith(prefix):
                names.add(line.split()[0][len(prefix):])
        return names

    def test_summary_lists_every_installed_skill(self):
        """A hardcoded list goes stale silently: 0.10.0 shipped gt-watch and the
        summary never mentioned it, so the installer denied a skill that existed."""
        p = self.install()
        self.assertOk(p)
        listed = self._summary_skills(p.stdout)
        on_disk = {d.name for d in (self.cache() / "skills").iterdir()
                   if (d / "SKILL.md").is_file()}
        self.assertEqual(listed, on_disk,
                         f"summary and installed skills disagree; "
                         f"missing from summary: {sorted(on_disk - listed)}, "
                         f"listed but absent: {sorted(listed - on_disk)}")

    def test_summary_lists_every_installed_wiki_skill(self):
        p = self.install()
        self.assertOk(p)
        listed = self._summary_skills(p.stdout, "/gt-wiki:")
        on_disk = {d.name for d in (self.cache("gt-wiki") / "skills").iterdir()
                   if (d / "SKILL.md").is_file()}
        self.assertEqual(listed, on_disk)

    def test_summary_omits_the_demo_when_it_is_not_installed(self):
        self.add_demo_module()
        self.config(install_demo="no")
        p = self.install()
        self.assertOk(p)
        self.assertFalse(self._summary_skills(p.stdout, "/gt-demo:"),
                         "gt-demo was listed as available but was not installed")
        listed = self._summary_skills(p.stdout)
        on_disk = {d.name for d in (self.cache() / "skills").iterdir()
                   if (d / "SKILL.md").is_file()}
        self.assertEqual(listed, on_disk)

    def test_summary_descriptions_are_not_cut_mid_word(self):
        """The first thing a new user reads; a mid-word cut reads as corruption."""
        p = self.install()
        self.assertOk(p)
        for line in p.stdout.splitlines():
            if line.strip().startswith("/gt:") and line.rstrip().endswith("\u2026"):
                self.assertRegex(line.rstrip(), r"[A-Za-z0-9)\]]\u2026$",
                                 f"description truncated mid-word: {line!r}")

    def test_manifest_version_mismatch_refuses(self):
        pj = self.src() / ".claude-plugin" / "plugin.json"
        d = json.loads(pj.read_text())
        d["version"] = "0.0.0"
        pj.write_text(json.dumps(d))
        p = self.install()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("version mismatch", p.stdout)
        self.assertFalse(self.plugins.exists())


class EnforcementHooksOnUpgrade(Sandbox):
    """install.sh must wire the ENFORCEMENT hooks when a vault is already configured.

    Until 0.12.1 it wired only its own seven. vault_init wired the other four, but only
    when a vault was CREATED — so an upgrade copied a newly shipped hook and never
    registered it. 0.12.0 shipped guard_vault_writes.sh and every existing machine
    reported it unwired at session start. A hook that ships inert is the failure the
    Core tier exists to close, and the component check calling that install "clean"
    made it worse.
    """

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "src" / "golden-thread-plugin"
        self.repo.mkdir(parents=True)
        shutil.copy2(INSTALL, self.repo / "install.sh")
        shutil.copytree(GT, self.repo / "golden-thread" / GT.name, ignore=IGNORE)
        shutil.copytree(WIKI, self.repo / "golden-thread-wiki" / WIKI.name, ignore=IGNORE)

    def wired(self, event):
        s = json.loads((self.home / ".claude" / "settings.json").read_text())
        return [h.get("command", "") for b in s.get("hooks", {}).get(event, [])
                for h in b.get("hooks", [])]

    def enforcement_hooks(self):
        from _harness import ENFORCEMENT_HOOKS
        return ENFORCEMENT_HOOKS

    def test_an_upgrade_wires_a_newly_shipped_enforcement_hook(self):
        v = self.make_vault()                      # a vault that exists BEFORE the install
        # The state an upgrade starts from: the new hook is in no settings.json entry.
        settings = self.home / ".claude" / "settings.json"
        data = json.loads(settings.read_text()) if settings.exists() else {}
        pre = data.get("hooks", {}).get("PreToolUse", [])
        data.setdefault("hooks", {})["PreToolUse"] = [
            b for b in pre
            if not any("guard_vault_writes" in (h.get("command") or "")
                       for h in b.get("hooks", []))]
        settings.write_text(json.dumps(data, indent=2))
        self.assertFalse([c for c in self.wired("PreToolUse") if "guard_vault_writes" in c],
                         "fixture failed to remove the entry")

        p = self.sh(self.repo / "install.sh", timeout=300)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

        cmds = self.wired("PreToolUse")
        self.assertTrue([c for c in cmds if "guard_vault_writes.sh" in c],
                        "install.sh left a shipped enforcement hook unwired:\n"
                        + "\n".join(cmds) + "\n" + p.stdout[-600:])

    def test_every_declared_hook_is_wired_after_installing_over_a_vault(self):
        self.make_vault()
        p = self.sh(self.repo / "install.sh", timeout=300)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for name in self.enforcement_hooks():
            found = any(name in c for ev in ("UserPromptSubmit", "Stop", "PreToolUse")
                        for c in self.wired(ev))
            self.assertTrue(found, f"{name} is declared but wired nowhere")

    # test_install_without_a_vault_still_succeeds and
    # test_without_a_vault_the_installer_explains_the_unwired_hooks were REMOVED in
    # 0.12.2, not silently: they asserted that a vault-less install finishes quietly,
    # which is precisely the behaviour that shipped inert hooks. The replacement
    # contract (stop with exit 4, say what is needed, invent nothing) is covered by
    # VaultIsPartOfTheInstall below.

    def test_with_a_vault_it_does_not_print_the_no_vault_notice(self):
        self.make_vault()
        p = self.sh(self.repo / "install.sh", timeout=300)
        self.assertNotIn("No vault configured", p.stdout,
                         "a wired install must not warn about a vault that exists")

    def test_a_second_install_reports_already_wired(self):
        self.make_vault()
        self.sh(self.repo / "install.sh", timeout=300)
        p = self.sh(self.repo / "install.sh", timeout=300)
        self.assertEqual(p.returncode, 0)
        self.assertIn("already wired", p.stdout,
                      "a repeat install should say it changed nothing, not re-report a fix")


class UpgradeLeavesOrderAndModesAsAFreshInstall(Sandbox):
    """Requirement R1 (0.15.0): hook ORDER and file MODES do not depend on history.

    Validated 2026-09-14: an upgrade from 0.13.0/0.14.0 left guard_protected_paths.sh last
    in PreToolUse (first on a fresh install), kept 0755 hooks and 0644 plugin.json where a
    fresh install from an untracked 0700 source tree gave 0711 and 0700 -- `cp` keeps an
    existing file's mode and gives a new file the source's.
    """

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "src" / "golden-thread-plugin"
        self.repo.mkdir(parents=True)
        shutil.copy2(INSTALL, self.repo / "install.sh")
        for src, dst in ((GT, self.repo / "golden-thread" / GT.name),
                         (WIKI, self.repo / "golden-thread-wiki" / WIKI.name)):
            shutil.copytree(src, dst, ignore=IGNORE)
        self.py(self.repo / "golden-thread" / GT.name / "scripts" / "gt_components.py",
                "manifest", self.repo / "golden-thread" / GT.name)
        # The source as an untracked OneDrive checkout leaves it: every file 0700.
        for p in self.repo.rglob("*"):
            if p.is_file():
                p.chmod(0o700)
        self.user_hook = {"hooks": [{"type": "command", "command": "echo users-own"}]}
        settings = self.home / ".claude" / "settings.json"
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [self.user_hook]}}, indent=2))

    def install(self):
        p = self.sh(self.repo / "install.sh", timeout=600)
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-2000:])
        return p

    def hooks(self):
        return json.loads((self.home / ".claude" / "settings.json").read_text())["hooks"]

    def key_orders(self):
        """Key ORDER, which dict equality ignores: hook events, enabledPlugins, and the
        plugins in installed_plugins.json (0.15.0: each differed between upgrade and fresh)."""
        claude = self.home / ".claude"
        s = json.loads((claude / "settings.json").read_text())
        inst = json.loads((claude / "plugins" / "installed_plugins.json").read_text())
        return (list(s.get("hooks", {})), list(s.get("enabledPlugins", {})),
                list(inst.get("plugins", {})))

    def modes(self):
        claude = self.home / ".claude"
        # The whole marketplace dir, so marketplace.json itself is covered (0.15.0: an
        # upgrade kept a 0600 copy), and no bytecode may be left in anything installed.
        roots = [claude / "plugins" / "cache" / "golden-thread-plugin",
                 claude / "plugins" / "marketplaces" / "golden-thread-plugin"]
        out = {}
        for root in roots:
            for p in [root, *root.rglob("*")]:
                self.assertNotIn("__pycache__", p.parts, "bytecode left in an installed tree")
                out[str(p.relative_to(claude))] = p.stat().st_mode & 0o777
        for p in (claude / "golden-thread" / "hooks").iterdir():
            if p.is_file():
                out[str(p.relative_to(claude))] = p.stat().st_mode & 0o777
        return out

    def test_order_and_modes_converge_and_follow_one_rule(self):
        self.make_vault()
        self.install()
        fresh_hooks, fresh_modes, fresh_orders = self.hooks(), self.modes(), self.key_orders()

        # The rule, whatever the 0700 source said.
        for rel, mode in fresh_modes.items():
            p = self.home / ".claude" / rel
            want = 0o755 if (p.is_dir() or rel.endswith((".sh", ".py"))) else 0o644
            self.assertEqual(oct(mode), oct(want), rel)
        pre = [h["command"] for b in fresh_hooks["PreToolUse"] for h in b["hooks"]]
        self.assertEqual(pre[0], "echo users-own", "the user's own hook stays first")
        self.assertIn("guard_protected_paths.sh", pre[1])

        # The history an older release leaves: gt's entries in another order, the user's
        # hook moved after them, files at other modes -- and a file of the user's own.
        settings = self.home / ".claude" / "settings.json"
        data = json.loads(settings.read_text())
        data["hooks"]["PreToolUse"] = list(reversed(data["hooks"]["PreToolUse"]))
        data["hooks"]["SessionStart"] = list(reversed(data["hooks"]["SessionStart"]))
        data["hooks"] = dict(reversed(list(data["hooks"].items())))
        data["enabledPlugins"] = dict(reversed(list(data.get("enabledPlugins", {}).items())))
        settings.write_text(json.dumps(data, indent=2))
        inst_p = self.home / ".claude" / "plugins" / "installed_plugins.json"
        inst = json.loads(inst_p.read_text())
        inst["plugins"] = dict(reversed(list(inst["plugins"].items())))
        inst_p.write_text(json.dumps(inst, indent=2))
        # bytecode a python run (or an older release) left in the installed trees
        (self.home / ".claude" / "plugins" / "cache" / "golden-thread-plugin" / "gt" / GT.name
         / "scripts" / "__pycache__").mkdir(parents=True, exist_ok=True)
        self.assertNotEqual(self.key_orders(), fresh_orders, "fixture must scramble key order")
        for rel in fresh_modes:
            p = self.home / ".claude" / rel
            if p.is_file():
                p.chmod(0o644 if rel.endswith((".sh", ".py")) else 0o600)
        mine = self.home / ".claude" / "golden-thread" / "hooks" / "users_own_tool.py"
        mine.write_text("# mine\n")
        mine.chmod(0o600)
        self.assertNotEqual(self.hooks(), fresh_hooks, "fixture must diverge first")

        self.install()
        self.assertEqual(self.hooks(), fresh_hooks, "hook order depends on history")
        self.assertEqual(self.key_orders(), fresh_orders, "JSON key order depends on history")
        now = self.modes()
        now.pop("golden-thread/hooks/users_own_tool.py", None)
        self.assertEqual(now, fresh_modes, "file modes depend on history")
        self.assertEqual(mine.stat().st_mode & 0o777, 0o600, "a user's file is not ours to chmod")


class VaultIsPartOfTheInstall(Sandbox):
    """An install that ends with no vault ends with the enforcement hooks inert.

    0.12.2 makes finishing possible in one command, and makes the unfinished case
    LOUD rather than silent — including for a non-interactive caller (an agent, a
    pipe, CI), which must not have a directory invented for it.
    """

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "src" / "golden-thread-plugin"
        self.repo.mkdir(parents=True)
        shutil.copy2(INSTALL, self.repo / "install.sh")
        shutil.copytree(GT, self.repo / "golden-thread" / GT.name, ignore=IGNORE)
        shutil.copytree(WIKI, self.repo / "golden-thread-wiki" / WIKI.name, ignore=IGNORE)

    def run_install(self, *args):
        return self.sh(self.repo / "install.sh", *args, timeout=300)

    def guard_entries(self):
        s = self.home / ".claude" / "settings.json"
        if not s.exists():
            return []
        d = json.loads(s.read_text())
        return [h.get("command", "") for bl in d.get("hooks", {}).values() for b in bl
                for h in b.get("hooks", []) if "guard_vault_writes" in (h.get("command") or "")]

    # -- the non-interactive case: stop and ask, never guess ----------------------
    def test_no_vault_non_interactive_stops_with_exit_4(self):
        p = self.run_install()
        self.assertEqual(p.returncode, 4,
                         "a non-interactive install with no vault must stop, not finish "
                         "silently with inert hooks:\n" + p.stdout[-500:])
        self.assertIn("INSTALL INCOMPLETE", p.stdout)

    def test_it_tells_the_caller_to_ask_the_user(self):
        """The caller is often an agent; the decision is the user's."""
        p = self.run_install()
        self.assertIn("ASK THE USER", p.stdout)
        self.assertIn("--vault", p.stdout, "it must name the flag that finishes the job")

    def test_it_does_not_invent_a_vault(self):
        self.run_install()
        self.assertFalse((self.home / "Documents" / "GoldenThread").exists(),
                         "the installer created a vault nobody asked for")
        self.assertFalse((self.home / ".claude" / "vault-config.json").exists(),
                         "the installer claimed the global vault config unasked")

    # -- --vault finishes everything ---------------------------------------------
    def test_vault_flag_creates_connects_and_wires(self):
        target = self.tmp / "myvault"
        p = self.run_install("--vault", str(target))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((target / "Projects").is_dir(), "no vault was created")
        self.assertTrue(self.guard_entries(),
                        "the vault was created but the enforcement hooks are not wired")

    def test_a_fresh_install_measures_the_machine(self):
        """The profile must exist after installing onto a machine that had nothing.

        0.12.5 wrote it from a step that ran BEFORE the vault was configured, so on a
        fresh machine there was no vault-config.json to write into and the step silently
        skipped -- the profile appeared only where one already existed. Every other check
        passed: the files arrived, the hooks wired, the installer said nothing was wrong.
        So this asserts the VALUE, not the delivery. `parallel_max: auto` resolves through
        it, which is the whole reason it is measured at install time.
        """
        target = self.tmp / "freshvault"
        p = self.run_install("--vault", str(target))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        cfg = json.loads((self.home / ".claude" / "vault-config.json").read_text())
        prof = cfg.get("parallel_profile")
        self.assertIsInstance(prof, dict,
                              "a fresh install must record parallel_profile; got %r" % prof)
        for key in ("cores", "cpu_max", "io_max", "host", "detected_at"):
            self.assertIn(key, prof, "profile is missing %s" % key)
        self.assertGreaterEqual(prof["cpu_max"], 1)
        self.assertGreaterEqual(prof["io_max"], prof["cpu_max"],
                                "I/O work may use at least as many workers as CPU work")

    def test_reinstalling_keeps_a_ceiling_the_user_chose(self):
        """The profile is hardware; parallel_max is a preference. Only one is rewritten."""
        target = self.tmp / "keepvault"
        self.assertEqual(self.run_install("--vault", str(target)).returncode, 0)
        cfgpath = self.home / ".claude" / "vault-config.json"
        cfg = json.loads(cfgpath.read_text())
        cfg["parallel_max"] = "2"
        cfg["parallel_work"] = "off"
        cfgpath.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        self.assertEqual(self.run_install("--vault", str(target)).returncode, 0)
        after = json.loads(cfgpath.read_text())
        self.assertEqual(after.get("parallel_max"), "2",
                         "re-running the installer must not undo a ceiling somebody set")
        self.assertEqual(after.get("parallel_work"), "off")
        self.assertIsInstance(after.get("parallel_profile"), dict)

    def test_vault_flag_connects_an_existing_vault(self):
        existing = self.make_vault()
        (self.home / ".claude" / "vault-config.json").unlink()
        p = self.run_install("--vault", str(existing))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        cfg = json.loads((self.home / ".claude" / "vault-config.json").read_text())
        self.assertEqual(cfg["vault_path"], str(existing.resolve()))

    def test_gt_vault_env_is_the_same_as_the_flag(self):
        target = self.tmp / "envvault"
        p = self.sh(self.repo / "install.sh", timeout=300,
                    env=dict(self.env, GT_VAULT=str(target)))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((target / "Projects").is_dir())

    # -- --no-vault is a deliberate choice, not a failure -------------------------
    def test_no_vault_flag_succeeds_and_explains(self):
        p = self.run_install("--no-vault")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("NOT wired", p.stdout)
        self.assertIn("gt-init", p.stdout, "it must say what will wire them later")

    # -- the existing contract must not regress ----------------------------------
    def test_version_argument_still_works(self):
        p = self.run_install(GT.name, "--no-vault")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(GT.name, p.stdout)

    def test_help_describes_the_real_options(self):
        p = self.run_install("--help")
        self.assertEqual(p.returncode, 0)
        for expected in ("--vault", "--no-vault", "exit 4"):
            self.assertIn(expected, p.stdout, f"--help does not mention {expected}")
        self.assertNotIn("git checkout", p.stdout,
                         "--help is printing a different comment block from the script")

    def test_unknown_option_is_refused(self):
        p = self.run_install("--wat")
        self.assertNotEqual(p.returncode, 0)


class ObsidianPointer(Sandbox):
    """A new vault explains how to look at it. The question is asked while standing in
    the folder, often on a machine that never cloned the plugin — so the answer lives
    in the vault, not only in the plugin's docs."""

    def test_new_vault_carries_the_obsidian_file(self):
        v = self.make_vault()
        f = v / "OPEN-IN-OBSIDIAN.md"
        self.assertTrue(f.is_file(), "a new vault does not say how to open it")
        text = f.read_text()
        self.assertIn(str(v), text, "the vault's own path was not substituted in")
        self.assertNotIn("{{VAULT_PATH}}", text, "an unsubstituted placeholder shipped")

    def test_it_names_the_plugin_the_conventions_depend_on(self):
        """Dataview is not decoration: [p:: 1] inline fields ARE its syntax."""
        text = (self.make_vault() / "OPEN-IN-OBSIDIAN.md").read_text()
        self.assertIn("Dataview", text)
        self.assertIn("[p::", text, "it must show the syntax that motivates the plugin")

    def test_it_warns_that_generated_files_are_not_hand_edited(self):
        text = (self.make_vault() / "OPEN-IN-OBSIDIAN.md").read_text()
        for name in ("TASKS.md", "log.md", "decisions.md"):
            self.assertIn(name, text,
                          f"{name} is generated; editing it in Obsidian loses the edit")

    def test_obsidian_is_presented_as_optional(self):
        text = (self.make_vault() / "OPEN-IN-OBSIDIAN.md").read_text()
        self.assertIn("optional", text.lower(),
                      "nothing here requires Obsidian; saying so prevents a false dependency")


class WiringDoesNotDependOnGit(Sandbox):
    """The enforcement hooks must be wired whenever a vault EXISTS.

    Reported from the other machine, 2026-09-12: still unwired after installing 0.12.2.
    0.12.1 put the wiring call inside the block gated on the vault being a git repo, so
    a vault that is not a repo installed with the hooks inert AND was reported as "no
    vault at all". Git decides whether the ATTRIBUTION hooks can be wired; it has
    nothing to do with an entry in settings.json. Same bug, second branch.
    """

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "src" / "golden-thread-plugin"
        self.repo.mkdir(parents=True)
        shutil.copy2(INSTALL, self.repo / "install.sh")
        shutil.copytree(GT, self.repo / "golden-thread" / GT.name, ignore=IGNORE)
        shutil.copytree(WIKI, self.repo / "golden-thread-wiki" / WIKI.name, ignore=IGNORE)

    def configure(self, vault, git=False):
        vault.mkdir(parents=True, exist_ok=True)
        (vault / "Projects").mkdir(exist_ok=True)
        (vault / "index.md").write_text("# index\n")
        if git:
            self.git_init(vault)
        cfg = self.home / ".claude" / "vault-config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"vault_path": str(vault)}))

    def guards(self):
        f = self.home / ".claude" / "settings.json"
        if not f.exists():
            return []
        d = json.loads(f.read_text())
        return [h.get("command", "") for bl in d.get("hooks", {}).values() for b in bl
                for h in b.get("hooks", []) if "guard_vault_writes" in (h.get("command") or "")]

    def test_wired_when_the_vault_is_a_git_repo(self):
        self.configure(self.tmp / "v", git=True)
        p = self.sh(self.repo / "install.sh", timeout=300)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(self.guards(), "not wired for a git vault")

    def test_wired_when_the_vault_is_NOT_a_git_repo(self):
        self.configure(self.tmp / "v", git=False)
        p = self.sh(self.repo / "install.sh", timeout=300)
        self.assertEqual(p.returncode, 0,
                         "an install with a configured vault must not exit 4:\n"
                         + p.stdout[-400:])
        self.assertTrue(self.guards(),
                        "a vault that is not a git repo left the enforcement hooks inert")

    def test_a_non_git_vault_is_not_reported_as_no_vault(self):
        self.configure(self.tmp / "v", git=False)
        p = self.sh(self.repo / "install.sh", timeout=300)
        self.assertNotIn("INSTALL INCOMPLETE", p.stdout,
                         "a configured vault was reported as missing")

    def test_still_exit_4_when_there_really_is_no_vault(self):
        p = self.sh(self.repo / "install.sh", timeout=300)
        self.assertEqual(p.returncode, 4)
        self.assertFalse(self.guards())


if __name__ == "__main__":
    unittest.main()


class GtPathsIsShippedOnce(InstallTest):
    """The end-to-end form of the 2026-09-12 report from the other machine.

    It saw `hooks/gt_paths.py` reported as drifted at every session start and filed a
    request to promote its installed copy into the plugin. The installed copy was already
    correct; what was wrong was that the release shipped gt_paths.py TWICE -- from hooks/
    (a 0.12.2-era copy without the gated_by/budget_from keys) and from scripts/ (current)
    -- both landing on one destination, so the manifest had to disagree with one of them.

    These assert the two things that together make the report impossible to reproduce:
    the installed file has the keys, and the drift check reads clean about it.
    """
    def test_the_installed_copy_has_the_keys_the_core_rules_need(self):
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        landed = self.home / ".claude" / "golden-thread" / "hooks" / "gt_paths.py"
        self.assertTrue(landed.is_file())
        text = landed.read_text(encoding="utf-8")
        for key in ("gated_by", "budget_from"):
            self.assertIn(key, text,
                          "%s is how a rule names the setting that governs it; without it "
                          "inject_core_rules.sh would have to hardcode setting names" % key)

    def test_drift_reads_clean_about_gt_paths_after_a_fresh_install(self):
        self.assertEqual(self.install().returncode, 0)
        comp = self.home / ".claude" / "golden-thread" / "hooks" / "gt_components.py"
        p = self.py(comp, "check", self.repo / "golden-thread" / GT.name)
        self.assertNotIn("gt_paths", p.stdout,
                         "a freshly installed tree must not report drift on gt_paths.py:\n"
                         + p.stdout)


class ManifestMismatch(InstallTest):
    """install.sh compares the files it is about to install against MANIFEST.json.

    Requested 2026-09-11 (2026-09-11-install-refuse-stale-manifest); the refuse-or-warn
    question it existed to settle was decided by the owner on 2026-09-12: refuse for files
    that EXECUTE, warn for files that are only copied, and treat a developer's uncommitted
    edit as the warning case.

    The sandbox copy is deliberately NOT a git tree, which makes it the "clean checkout on
    a second machine" case -- the one the request is actually about. The local-edit
    exemption is tested separately, with a real git repo.
    """
    HOOK = "hooks/inject_core_rules.sh"
    TEMPLATE = "templates/core-rules/README.md"

    def shipped(self, rel):
        return self.repo / "golden-thread" / GT.name / rel

    def edit(self, rel, line="# edited without regenerating the manifest\n"):
        p = self.shipped(rel)
        p.write_text(p.read_text(encoding="utf-8") + line, encoding="utf-8")
        return p

    # -- the case it exists for ------------------------------------------------------
    def test_a_modified_executing_file_refuses_and_installs_nothing(self):
        self.edit(self.HOOK)
        p = self.install()
        self.assertEqual(p.returncode, 6,
                         "a modified hook must refuse with its own exit code\n" + p.stdout)
        self.assertIn("REFUSING TO INSTALL", p.stdout)
        self.assertIn(self.HOOK, p.stdout, "the refusal must name the file")
        self.assertIn("gt_components.py manifest", p.stdout,
                      "the refusal must name the command that fixes it")
        self.assertFalse(self.plugins.exists(),
                         "REFUSING means nothing was installed; the cache exists")

    def test_the_override_installs_the_modified_source(self):
        marker = "# deliberate local change\n"
        self.edit(self.HOOK, marker)
        p = self.install("--force-manifest-mismatch")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("overridden by --force-manifest-mismatch", p.stdout)
        landed = self.home / ".claude" / "golden-thread" / "hooks" / "inject_core_rules.sh"
        self.assertTrue(landed.is_file(), "the override must complete the install")
        self.assertIn(marker.strip(), landed.read_text(encoding="utf-8"),
                      "the modified file is what should have landed")

    # -- executes vs merely copied ---------------------------------------------------
    def test_a_modified_copied_file_warns_and_installs(self):
        self.edit(self.TEMPLATE)
        p = self.install()
        self.assertEqual(p.returncode, 0,
                         "a template is copied, not run: warn, do not refuse\n" + p.stdout)
        self.assertIn("Manifest mismatch in copied files", p.stdout)
        self.assertIn(self.TEMPLATE, p.stdout)
        self.assertTrue(self.plugins.exists(), "the install should have completed")

    # -- the developer loop ----------------------------------------------------------
    def test_an_uncommitted_edit_warns_instead_of_refusing(self):
        """Editing a script and installing to test it is the normal loop here.

        A gate that fires on the normal loop gets overridden by reflex and then ignored,
        so git's own answer decides: uncommitted or untracked means work in progress.
        """
        self.git_init(self.repo)                      # commit the tree as it stands
        self.edit(self.HOOK)                          # now an uncommitted modification
        p = self.install()
        self.assertEqual(p.returncode, 0,
                         "an uncommitted edit must not refuse\n" + p.stdout)
        self.assertIn("local edits present", p.stdout)
        self.assertIn(self.HOOK, p.stdout, "it still says which file differs")
        self.assertTrue(self.plugins.exists())

    # -- degenerate inputs -----------------------------------------------------------
    def test_a_clean_source_is_silent_about_the_manifest(self):
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for noise in ("REFUSING", "Manifest mismatch", "local edits present"):
            self.assertNotIn(noise, p.stdout,
                             "a matching tree must produce no mismatch report")

    def test_a_source_with_no_manifest_is_handled_explicitly(self):
        man = self.repo / "golden-thread" / GT.name / "MANIFEST.json"
        if man.exists():
            man.unlink()
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stdout + p.stderr,
                         "a missing manifest must not crash the installer")
        self.assertTrue(man.is_file(), "the installer writes one when it is absent")
