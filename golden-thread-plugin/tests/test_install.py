"""install.sh: installs gt + gt-wiki into HOME and wires the reporting hooks.

install.sh writes MANIFEST.json into the version dir it installs FROM, so every
run here uses a throwaway copy of the repo (install.sh + the newest version dirs)
and a sandbox HOME. Nothing touches the real repo or the real ~/.claude.

Contracts pinned here:
  * gt and gt-wiki land in ~/.claude/plugins/cache/... AND the marketplace, file for
    file; both are registered and enabled;
  * the seven hooks install.sh owns are registered in settings.json and verified, and
    point at files that exist in the sandbox;
  * install_demo=no leaves out exactly the demo skill, script and templates from
    BOTH cache and marketplace -- including a demo left by an earlier install;
  * a second run is idempotent; foreign settings survive; old gt caches are pruned;
  * a bad version request or a manifest/dir mismatch refuses before installing.
"""
import json
import shutil
import unittest
from pathlib import Path

from _harness import Sandbox, REPO, GT, WIKI

INSTALL = REPO / "install.sh"
DEMO = ("skills/gt-demo", "scripts/gt_demo.sh", "templates/demo-pizzabot")
GT_DIRS = (".claude-plugin", "skills", "scripts", "templates", "commands", "hooks")
WIKI_DIRS = (".claude-plugin", "skills", "scripts", "templates", "commands")
OWNED = {"SessionStart": {"gt_components.py", "gt_workers.py", "gt_version_check.py",
                          "gt_push_check.py", "gt_watch.py"},
         "PreCompact": {"gt_report_card.py"}, "SessionEnd": {"gt_report_card.py"}}
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


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
        return dest

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
        # demo is installed by default
        for d in DEMO:
            self.assertTrue((self.cache() / d).exists(), d)
        hooks_dir = self.home / ".claude" / "golden-thread" / "hooks"
        for f in ("inject_core_rules.sh", "validate_response.sh", "gt_paths.py", "gt_components.py"):
            self.assertTrue((hooks_dir / f).is_file(), f)
        self.assertTrue((self.src() / "MANIFEST.json").is_file())

    def registered(self):
        hooks = self.settings().get("hooks", {})
        return {ev: [h["command"] for block in blocks for h in block.get("hooks", [])]
                for ev, blocks in hooks.items()}

    def test_seven_hooks_registered_and_verified(self):
        p = self.install()
        self.assertOk(p)
        self.assertIn("Registered 7 hooks", p.stdout)
        self.assertIn("Verified hook wiring", p.stdout)
        self.assertNotIn("INCOMPLETE", p.stdout)
        reg = self.registered()
        self.assertEqual(sum(len(v) for v in reg.values()), 7, reg)
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

    def test_install_demo_no_omits_exactly_the_demo(self):
        for d in DEMO:
            self.assertTrue((self.src() / d).exists(), f"fixture: {d} missing from the source")
        self.config(install_demo="no")
        p = self.install()
        self.assertOk(p)
        self.assertIn("install_demo=no", p.stdout)
        self.assertFalse([l for l in p.stdout.splitlines() if l.startswith("  /gt:gt-demo")],
                         "summary lists the demo skill as installed")
        want = {f for f in files_under(self.src(), GT_DIRS) if not is_demo(f)}
        for where, root in (("cache", self.cache()), ("marketplace", self.market())):
            with self.subTest(where=where):
                for d in DEMO:
                    self.assertFalse((root / d).exists(), f"{where} still has {d}")
                self.assertEqual(files_under(root, GT_DIRS), want)
        # gt-wiki is untouched by the demo setting
        self.assertEqual(files_under(self.cache("gt-wiki"), WIKI_DIRS),
                         files_under(self.src("gt-wiki"), WIKI_DIRS))

    def test_install_demo_no_clears_a_demo_left_by_an_earlier_install(self):
        self.assertOk(self.install())
        self.assertTrue((self.cache() / DEMO[0]).exists())
        self.config(install_demo="no")
        self.assertOk(self.install())
        for root in (self.cache(), self.market()):
            for d in DEMO:
                self.assertFalse((root / d).exists(), f"{root}: {d} survived")

    def test_second_run_is_idempotent(self):
        self.assertOk(self.install())
        s1 = self.settings()
        files1 = files_under(self.cache(), GT_DIRS)
        p = self.install()
        self.assertOk(p)
        self.assertEqual(self.settings(), s1, "second run changed settings.json")
        self.assertEqual(sum(len(v) for v in self.registered().values()), 7, "hooks duplicated")
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
        self.assertEqual(len(self.registered()["SessionStart"]), 6)   # user hook + 5 gt SessionStart hooks
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

    def test_stale_manifest_still_installs(self):
        """Pins that this change did not quietly add a refusal.

        Refusing a source whose files disagree with its manifest is tracked separately
        as 2026-09-11-install-refuse-stale-manifest; it is a behaviour change and must
        not arrive as a side effect of the fix above.
        """
        victim = self.src() / "scripts" / "gt_settings.py"
        victim.write_text(victim.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
        p = self.install()
        self.assertOk(p, "an install with a stale manifest should still succeed today")

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
        self.config(install_demo="no")
        p = self.install()
        self.assertOk(p)
        self.assertNotIn("gt-demo", self._summary_skills(p.stdout),
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


if __name__ == "__main__":
    unittest.main()
