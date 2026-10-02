"""guard_foreign_checkout.sh / .py (0.18.1, 2026-09-11-guard-foreign-checkout-writes).

A commit or push inside a checkout the user DECLARED as another machine's is denied, naming
the checkout and the supported route. Everything else is untouched, and every uncertainty
fails open. Each acceptance criterion is one test below; all run against throwaway repos
under a temporary HOME, no network, no real vault.
"""
import json
import shutil

from _harness import Sandbox, HOOKS, SCRIPTS, GT, load_module

HOOK = "guard_foreign_checkout.sh"
ROUTE = "run copygt.sh on the owning machine"


class ForeignGuardBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.hooks = self.home / ".claude" / "golden-thread" / "hooks"
        self.hooks.mkdir(parents=True)
        for f in HOOKS.iterdir():
            if f.is_file():
                shutil.copy2(f, self.hooks / f.name)
        comp = load_module(SCRIPTS / "gt_components.py", "gt_components_for_foreign_guard")
        for name in ("gt_paths.py",) + tuple(comp.HOOK_DIR_SCRIPTS):
            if (SCRIPTS / name).is_file():
                shutil.copy2(SCRIPTS / name, self.hooks / name)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.foreign = self.git_init(self.tmp / "foreign")
        self.mine = self.git_init(self.tmp / "mine")
        self.declare([{"path": str(self.foreign), "label": "the build machine",
                       "route": ROUTE}])

    def declare(self, decl, raw=None):
        cfg = self.home / ".claude" / "vault-config.json"
        if raw is not None:
            cfg.write_text(raw)
            return
        data = {"vault_path": str(self.tmp)}
        if decl is not None:
            data["foreign_checkouts"] = decl
        cfg.write_text(json.dumps(data))

    def run_guard(self, command, cwd=None, tool="Bash"):
        payload = {"tool_name": tool, "tool_input": {"command": command},
                   "cwd": str(cwd or self.foreign), "hook_event_name": "PreToolUse"}
        p = self.sh(self.hooks / HOOK, input=json.dumps(payload))
        self.assertEqual(0, p.returncode, p.stderr)
        return p

    def assertDenied(self, p):
        self.assertTrue(p.stdout.strip(), "expected a deny, got no objection")
        d = json.loads(p.stdout)["hookSpecificOutput"]
        self.assertEqual("deny", d["permissionDecision"])
        return d["permissionDecisionReason"]

    def assertAllowed(self, p):
        self.assertEqual("", p.stdout.strip(),
                         "no objection must be NO output (never 'allow'): %s" % p.stdout)


class Denials(ForeignGuardBase):
    def test_commit_denied_naming_the_checkout(self):
        reason = self.assertDenied(self.run_guard("git commit -m 'release'"))
        self.assertIn(str(self.foreign.resolve()), reason)
        self.assertIn("git commit", reason)

    def test_push_denied(self):
        self.assertIn("git push", self.assertDenied(self.run_guard("git push origin main")))

    def test_message_names_the_supported_route(self):
        reason = self.assertDenied(self.run_guard("git push"))
        self.assertIn(ROUTE, reason)
        self.assertIn("the build machine", reason)
        self.assertIn("GT_FOREIGN_CHECKOUT=allow", reason)

    def test_default_route_when_none_declared(self):
        self.declare([str(self.foreign)])
        reason = self.assertDenied(self.run_guard("git push"))
        self.assertIn("Supported route:", reason)
        self.assertIn("machine that owns this checkout", reason)

    def test_subdirectory_matches_by_containment(self):
        sub = self.foreign / "a" / "b"
        sub.mkdir(parents=True)
        self.assertDenied(self.run_guard("git commit -am x", cwd=sub))

    def test_symlinked_cwd_does_not_slip_past(self):
        link = self.tmp / "link"
        link.symlink_to(self.foreign)
        self.assertDenied(self.run_guard("git push", cwd=link))

    def test_dash_C_and_cd_into_the_checkout_are_seen(self):
        self.assertDenied(self.run_guard("git -C %s commit -m x" % self.foreign, cwd=self.mine))
        self.assertDenied(self.run_guard("cd %s && git push" % self.foreign, cwd=self.mine))

    def test_claude_code_heredoc_commit_message_is_still_seen(self):
        cmd = ("git add -A && git commit -m \"$(cat <<'EOF'\nrelease; push it\n\nCo-Authored-By: x\n"
               "EOF\n)\"")
        self.assertDenied(self.run_guard(cmd))


class Allowances(ForeignGuardBase):
    def test_read_only_git_is_never_denied(self):
        for cmd in ("git status", "git diff", "git log --oneline -5", "git fetch origin",
                    "git commit --help", "git push -h"):
            self.assertAllowed(self.run_guard(cmd))

    def test_undeclared_repo_commit_allowed(self):
        self.assertAllowed(self.run_guard("git commit -m x", cwd=self.mine))
        self.assertAllowed(self.run_guard("git push", cwd=self.mine))

    def test_no_declaration_denies_nothing(self):
        self.declare(None)
        for cmd in ("git commit -m x", "git push", "git status"):
            self.assertAllowed(self.run_guard(cmd))

    def test_override_is_honoured_and_visible_in_the_command(self):
        cmd = "GT_FOREIGN_CHECKOUT=allow git push origin main"
        p = self.run_guard(cmd)
        self.assertAllowed(p)
        self.assertIn("GT_FOREIGN_CHECKOUT=allow", cmd)

    def test_unparseable_command_fails_open(self):
        for cmd in ("eval \"git push\"", "bash -c 'git commit -m x'", "cd $SOMEWHERE && git push",
                    "git commit -m 'unterminated", "echo $(git push)", "$GIT push"):
            self.assertAllowed(self.run_guard(cmd))

    def test_broken_config_fails_open(self):
        self.declare(None, raw='{"vault_path": "/x", "foreign_checkouts": [')
        self.assertAllowed(self.run_guard("git push"))
        self.declare([str(self.tmp / "does-not-exist")])
        self.assertAllowed(self.run_guard("git push", cwd=self.tmp))
        self.declare("not-a-list")
        self.assertAllowed(self.run_guard("git push"))

    def test_garbage_payload_and_other_tools_fail_open(self):
        p = self.sh(self.hooks / HOOK, input="{not json")
        self.assertEqual(0, p.returncode)
        self.assertAllowed(p)
        self.assertAllowed(self.run_guard("git push", tool="Write"))

    def test_setting_off_disables_it(self):
        cfg = self.home / ".claude" / "vault-config.json"
        data = json.loads(cfg.read_text())
        data["foreign_checkout_guard"] = "off"
        cfg.write_text(json.dumps(data))
        self.assertAllowed(self.run_guard("git push"))

    def test_file_writes_in_the_checkout_are_untouched(self):
        self.assertAllowed(self.run_guard("cp /etc/hosts %s/x && echo hi" % self.foreign))


class DeclarationCli(ForeignGuardBase):
    def cli(self, *args):
        return self.py(self.hooks / "guard_foreign_checkout.py", *args)

    def test_add_list_remove_round_trip(self):
        self.declare([])
        self.assertOk(self.cli("add", str(self.mine), "--label", "laptop", "--dry-run"))
        self.assertNotIn("mine", (self.home / ".claude" / "vault-config.json").read_text())
        self.assertOk(self.cli("add", str(self.mine), "--label", "laptop"))
        out = self.cli("list").stdout
        self.assertIn("active", out)
        self.assertIn("laptop", out)
        self.assertDenied(self.run_guard("git push", cwd=self.mine))
        self.assertOk(self.cli("remove", str(self.mine)))
        self.assertAllowed(self.run_guard("git push", cwd=self.mine))
        cfg = json.loads((self.home / ".claude" / "vault-config.json").read_text())
        self.assertEqual(str(self.tmp), cfg["vault_path"], "other keys must survive")


class Wiring(ForeignGuardBase):
    def test_registered_like_its_siblings(self):
        comp = load_module(SCRIPTS / "gt_components.py", "gt_components_for_foreign_wiring")
        regs = [r for r in comp.HOOK_REGISTRATIONS if r["script"] == HOOK]
        self.assertEqual(1, len(regs))
        self.assertEqual(("PreToolUse", "install.sh"), (regs[0]["event"], regs[0]["owner"]))
        settings = load_module(SCRIPTS / "gt_settings.py", "gt_settings_for_foreign")
        self.assertEqual("on", settings.SETTINGS["foreign_checkout_guard"]["default"])
        self.assertTrue((GT / "hooks" / "guard_foreign_checkout.py").is_file())
