"""install.sh prunes what the target release no longer ships, converging on a fresh install.

Added in 0.13.0. Until then the installer only ever added:
  * old gt-wiki cache versions lingered (gt caches were pruned, gt-wiki's never were);
  * a hook entry an older release wired and a newer one dropped stayed in settings.json;
  * a file an older release put in the hooks dir stayed there, unmentioned;
  * gt_upgrade was never consulted, so a vault migration pending since 0.12.0 went
    unnoticed across every later install.

Contracts pinned here:
  * stale gt-wiki caches are removed, the installed one kept;
  * a settings.json entry pointing INTO the gt hooks dir is removed when retired.json
    lists its script, or when gt registers that script under a different event; an
    unknown entry there is reported and kept; a user's own hook always survives;
  * a hooks-dir file is removed (after a backup) only when retired.json lists it and
    the release does not ship it; an unknown file is reported and kept;
  * vault upgrades: see test_install_vault_upgrade.py (applied when clean since 0.14.0);
  * installing over an old release (0.12.8, from git) converges on a fresh install.
"""
import hashlib
import json
import shutil

from _harness import Sandbox, REPO, GT, WIKI, latest_version_dir

INSTALL = REPO / "install.sh"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


class PruneBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "src" / "golden-thread-plugin"
        self.repo.mkdir(parents=True)
        shutil.copy2(INSTALL, self.repo / "install.sh")
        shutil.copytree(GT, self.src, ignore=IGNORE)
        shutil.copytree(WIKI, self.repo / "golden-thread-wiki" / WIKI.name, ignore=IGNORE)
        self.manifest()

    @property
    def src(self):
        return self.repo / "golden-thread" / GT.name

    @property
    def hooks_dir(self):
        return self.home / ".claude" / "golden-thread" / "hooks"

    @property
    def settings_path(self):
        return self.home / ".claude" / "settings.json"

    def manifest(self):
        self.assertOk(self.py(self.src / "scripts" / "gt_components.py", "manifest", self.src))

    def install(self, *args):
        if not any(a in ("--vault", "--no-vault") for a in args):
            args = (*args, "--no-vault")
        p = self.sh(self.repo / "install.sh", *args, timeout=300)
        self.assertOk(p, "install.sh failed")
        return p

    def commands(self):
        d = json.loads(self.settings_path.read_text())
        return {ev: [h.get("command", "") for b in blocks for h in b.get("hooks", [])]
                for ev, blocks in d.get("hooks", {}).items()}


class PrunesStaleCaches(PruneBase):
    def test_old_gt_wiki_caches_are_removed_and_the_installed_one_kept(self):
        root = self.home / ".claude" / "plugins" / "cache" / "golden-thread-plugin" / "gt-wiki"
        for v in ("0.1.0", "0.0.9"):
            (root / v).mkdir(parents=True)
            (root / v / "stale.txt").write_text("x")
        p = self.install()
        self.assertEqual(sorted(d.name for d in root.iterdir()), [WIKI.name])
        self.assertIn("Removed superseded gt-wiki cache", p.stdout)


class RetiredBase(PruneBase):
    def retire(self, files=(), registrations=()):
        """Write the fixture copy's retired.json -- the release's record of what it dropped."""
        (self.src / "retired.json").write_text(json.dumps({
            "hook_files": [{"source": "hooks/" + f, "installed_as": f} for f in files],
            "hook_registrations": list(registrations)}, indent=2))
        self.manifest()


class PrunesDroppedHookEntries(RetiredBase):
    def seed_settings(self):
        old = str(self.hooks_dir / "gt_retired_check.py")
        self.settings_path.write_text(json.dumps({"hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "python3 %s check --hook" % old}]},
                # a user's hook sharing a block with a retired gt entry
                {"hooks": [{"type": "command", "command": str(self.hooks_dir / "gt_old.sh")},
                           {"type": "command", "command": "echo mine"}]},
            ],
            # a user's hook that merely MENTIONS a retired script name, outside the dir
            "Stop": [{"hooks": [{"type": "command",
                                 "command": "python3 /opt/me/gt_retired_check.py"}]}],
            # a script gt registers, wired by an older release under an event it no
            # longer uses -- gt owns the script, so the stray entry goes
            "Notification": [{"hooks": [{"type": "command",
                                         "command": "python3 ~/.claude/golden-thread/hooks/gt_workers.py check"}]}],
            # in the hooks dir, but gt has no record of it: unknown, left alone
            "UserPromptSubmit": [{"hooks": [{"type": "command",
                                             "command": str(self.hooks_dir / "my_own.sh")}]}],
        }}, indent=2))

    def test_retired_and_misplaced_entries_are_removed_and_reported(self):
        self.retire(registrations=("gt_retired_check.py", "gt_old.sh"))
        self.seed_settings()
        p = self.install()
        cmds = self.commands()
        flat = [c for v in cmds.values() for c in v]
        self.assertFalse([c for c in flat if "gt_retired_check.py" in c and "/opt/me/" not in c],
                         flat)
        self.assertFalse([c for c in flat if "gt_old.sh" in c], flat)
        self.assertIn("Removed 3 hook entries", p.stdout)
        for name in ("SessionStart/gt_retired_check.py", "SessionStart/gt_old.sh",
                     "Notification/gt_workers.py"):
            self.assertIn(name, p.stdout)
        self.assertNotIn("Notification", cmds, "an event emptied by the prune should go")
        backups = list((self.home / ".claude" / "golden-thread" / "backups")
                       .glob("settings.json.*.pre-prune"))
        self.assertTrue(backups, "settings.json was not backed up before pruning")
        self.assertIn("gt_retired_check.py", backups[0].read_text())

    def test_user_hooks_and_unknown_entries_survive(self):
        self.retire(registrations=("gt_retired_check.py", "gt_old.sh"))
        self.seed_settings()
        p = self.install()
        cmds = self.commands()
        self.assertIn("echo mine", cmds["SessionStart"])
        self.assertEqual(cmds["Stop"], ["python3 /opt/me/gt_retired_check.py"])
        self.assertEqual(cmds["UserPromptSubmit"][0], str(self.hooks_dir / "my_own.sh"))
        self.assertIn("Unknown hook entry pointing into the gt hooks dir, left in place", p.stdout)
        self.assertIn("UserPromptSubmit/my_own.sh", p.stdout)

    def test_without_a_retired_record_nothing_unknown_is_removed(self):
        self.retire()
        self.seed_settings()
        p = self.install()
        flat = [c for v in self.commands().values() for c in v]
        self.assertTrue([c for c in flat if "gt_retired_check.py" in c and "/opt/me/" not in c])
        self.assertTrue([c for c in flat if "gt_old.sh" in c])
        self.assertIn("Removed 1 hook entry", p.stdout)          # only the misplaced gt_workers

    def test_current_registrations_are_not_pruned(self):
        v = self.make_vault()
        self.config(vault_path=str(v))
        self.install()
        p = self.install()
        self.assertNotIn("no longer ships (backup", p.stdout)
        from _harness import ENFORCEMENT_HOOKS
        flat = [c for v in self.commands().values() for c in v]
        for name in ENFORCEMENT_HOOKS:
            self.assertTrue([c for c in flat if name in c], f"{name} pruned or unwired")

    def test_the_shipped_retired_json_is_well_formed(self):
        data = json.loads((GT / "retired.json").read_text())
        self.assertIsInstance(data.get("hook_registrations"), list)
        for e in data.get("hook_files"):
            self.assertTrue(e.get("installed_as") and e.get("source"), e)


class RetiresHookFiles(RetiredBase):
    def test_a_retired_file_is_backed_up_and_removed(self):
        self.retire(files=("guard_retired.sh",))
        self.hooks_dir.mkdir(parents=True)
        (self.hooks_dir / "guard_retired.sh").write_text("#!/bin/sh\n")
        p = self.install()
        self.assertFalse((self.hooks_dir / "guard_retired.sh").exists())
        self.assertIn("removed 1 file(s) this release retired", p.stdout)
        bak = list((self.home / ".claude" / "golden-thread" / "backups")
                   .glob("hooks-retired.*/guard_retired.sh"))
        self.assertTrue(bak, "a retired hook file was removed without a backup")

    def test_an_unknown_file_is_reported_and_left_in_place(self):
        self.retire(files=("guard_retired.sh",))
        self.hooks_dir.mkdir(parents=True)
        extra = self.hooks_dir / "my_local_guard.sh"
        extra.write_text("#!/bin/sh\n")
        p = self.install()
        self.assertTrue(extra.is_file(), "an unknown hook file was deleted")
        self.assertIn("unknown file(s), not shipped by this release, left in place", p.stdout)
        self.assertIn("my_local_guard.sh", p.stdout)

    def test_a_retired_name_the_release_still_ships_is_kept(self):
        # hooks/gt_paths.py was retired in 0.12.8, but scripts/gt_paths.py still installs
        # to the same destination -- the real retired.json carries exactly this case.
        self.retire(files=("gt_paths.py",))
        self.install()
        self.assertTrue((self.hooks_dir / "gt_paths.py").is_file())

    def test_a_clean_hooks_dir_reports_nothing(self):
        p = self.install()
        self.assertNotIn("left in place", p.stdout)
        self.assertNotIn("this release retired", p.stdout)


# ReportsVaultUpgrades moved to test_install_vault_upgrade.py in 0.14.0, when install.sh
# began APPLYING pending upgrades to a clean vault instead of only reporting them.


OLD_RELEASE = ("0f82843", "0.12.8", "0.1.2")     # commit, gt, gt-wiki at that commit


class UpgradeConverges(Sandbox):
    """One run of install.sh over an OLD release leaves what a fresh install leaves.

    Owner requirement, 2026-09-14: someone on 0.12.8 who upgrades straight to the newest
    release gets every new feature at once and no legacy item stuck. Compared: the gt
    hook entries in settings.json, the hooks dir (names AND content), and the plugin
    cache -- with a user's own hook entry and a user's own file in the hooks dir kept.
    The old release comes from git history, so this skips where there is none (gt-src).
    """

    def setUp(self):
        super().setUp()
        self.new_repo = self.tmp / "new" / "golden-thread-plugin"
        self.new_repo.mkdir(parents=True)
        shutil.copy2(INSTALL, self.new_repo / "install.sh")
        shutil.copytree(GT, self.new_repo / "golden-thread" / GT.name, ignore=IGNORE)
        shutil.copytree(WIKI, self.new_repo / "golden-thread-wiki" / WIKI.name, ignore=IGNORE)
        # 0.14.0: every module ships beside gt; both installs must lay them down (default on)
        try:
            demo = latest_version_dir(REPO / "golden-thread-demo")
        except (RuntimeError, OSError):
            demo = None
        if demo is not None:
            shutil.copytree(demo, self.new_repo / "golden-thread-demo" / demo.name, ignore=IGNORE)
        self.has_demo = demo is not None
        self.assertOk(self.py(self.new_repo / "golden-thread" / GT.name / "scripts"
                              / "gt_components.py", "manifest",
                              self.new_repo / "golden-thread" / GT.name))

    def old_repo(self):
        commit, gt, wiki = OLD_RELEASE
        dest = self.tmp / "old"
        dest.mkdir()
        if not shutil.which("git"):
            self.skipTest("git not installed")
        paths = ["golden-thread-plugin/install.sh",
                 "golden-thread-plugin/golden-thread/%s" % gt,
                 "golden-thread-plugin/golden-thread-wiki/%s" % wiki]
        arch = self.run_cmd(["git", "-C", REPO.parent, "archive", "--format=tar",
                             "-o", self.tmp / "old.tar", commit, *paths])
        if arch.returncode != 0:
            self.skipTest("release %s not in git history here: %s" % (gt, arch.stderr.strip()))
        self.assertOk(self.run_cmd(["tar", "-xf", self.tmp / "old.tar", "-C", dest]))
        return dest / "golden-thread-plugin"

    def run_install(self, repo, home, *args):
        p = self.run_cmd(["bash", repo / "install.sh", *args], env={"HOME": str(home)},
                         timeout=300)
        self.assertOk(p, "install.sh (%s) failed" % repo)
        return p

    def state(self, home, vault):
        def norm(text):
            return text.replace(str(vault), "<VAULT>").replace(str(home), "<HOME>")
        settings = json.loads((home / ".claude" / "settings.json").read_text())
        gt_entries = sorted(
            (ev, norm(json.dumps(h, sort_keys=True)))
            for ev, blocks in settings.get("hooks", {}).items() for b in blocks
            for h in b.get("hooks", []) if "golden-thread" in h.get("command", ""))
        hooks = home / ".claude" / "golden-thread" / "hooks"
        hook_files = {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                      for f in hooks.iterdir() if f.is_file() and f.name != "user_note.sh"}
        cache = home / ".claude" / "plugins" / "cache" / "golden-thread-plugin"
        cache_files = sorted(str(f.relative_to(cache)) for f in cache.rglob("*")
                             if f.is_file() and "__pycache__" not in f.parts)
        return {"entries": gt_entries, "hook_files": hook_files, "cache": cache_files}

    def test_upgrading_from_an_old_release_matches_a_fresh_install(self):
        old = self.old_repo()
        home_up, vault_up = self.home, self.tmp / "vault-up"
        self.run_install(old, home_up, "--vault", vault_up)
        # the user's own additions, made while on the old release
        s = home_up / ".claude" / "settings.json"
        data = json.loads(s.read_text())
        data["hooks"].setdefault("Stop", []).append(
            {"hooks": [{"type": "command", "command": "echo users-own-hook"}]})
        s.write_text(json.dumps(data, indent=2))
        user_file = home_up / ".claude" / "golden-thread" / "hooks" / "user_note.sh"
        user_file.write_text("#!/bin/sh\n")
        self.run_install(self.new_repo, home_up)

        home_fresh, vault_fresh = self.tmp / "home-fresh", self.tmp / "vault-fresh"
        (home_fresh / ".claude").mkdir(parents=True)
        self.run_install(self.new_repo, home_fresh, "--vault", vault_fresh)

        up, fresh = self.state(home_up, vault_up), self.state(home_fresh, vault_fresh)
        self.assertEqual(up["entries"], fresh["entries"], "gt hook entries differ")
        self.assertEqual(up["hook_files"], fresh["hook_files"], "hooks dir differs")
        self.assertEqual(up["cache"], fresh["cache"], "plugin cache differs")
        plugins = {f.split("/", 1)[0] for f in fresh["cache"]}
        self.assertIn("gt-wiki", plugins)
        if self.has_demo:
            self.assertIn("gt-demo", plugins, "the demo module was not installed")
            self.assertFalse([f for f in up["cache"] if f.startswith("gt/") and "demo" in f],
                             "the upgraded gt plugin still carries the demo")
        self.assertTrue(user_file.is_file(), "a user's file in the hooks dir was removed")
        stop = [h["command"] for b in json.loads(s.read_text())["hooks"]["Stop"]
                for h in b["hooks"]]
        self.assertIn("echo users-own-hook", stop)
