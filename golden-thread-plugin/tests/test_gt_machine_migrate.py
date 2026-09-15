"""gt_machine_migrate.py -- one-time MACHINE migrations (Requirement R1).

Contract:
  * Pending = not recorded applied AND needed() true. needed() reads the machine, so a
    recorded-but-needed-again migration is pending again, and a machine that never
    needed one is not touched.
  * `status` exits 0 pending or not; 2 when a needed() raises or state is unreadable.
  * `run` applies in order, stops at the first failure (exit 1), and a re-run after the
    fix applies the rest. --dry-run writes nothing.
  * State is written atomically; files are backed up before they change.
  * install-choices-from-vault-config records the demo choice and leaves
    vault-config.json's install_demo untouched.
  * farm-kept-for-upgraders (0.15.0) records farm=on only for a machine whose previous gt
    shipped /gt:gt-farm (0.9.4 .. 0.14.x) and has no farm choice; the previous release comes
    from --previous-release / GT_PREVIOUS_RELEASE, else installed_plugins.json naming
    another gt, else machine-state.json last_release. A `watch` or `report_card` setting is
    never turned into a module choice.
"""
import json
import textwrap

from _harness import Sandbox, SCRIPTS, GT

MM = SCRIPTS / "gt_machine_migrate.py"
REAL_ID = "install-choices-from-vault-config"


class MachineBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.gt = self.home / ".claude" / "golden-thread"
        self.gt.mkdir(parents=True)
        self.state_path = self.gt / "machine-state.json"
        self.choices = self.gt / "install-choices.json"

    def mm(self, *args, env=None):
        return self.py(MM, "--home", str(self.home), *args, "--release", str(GT), env=env)

    def state(self):
        return json.loads(self.state_path.read_text())

    def tree(self):
        return {str(p.relative_to(self.home)): (p.read_bytes() if p.is_file() else None)
                for p in sorted(self.home.rglob("*"))}


class RealMigration(MachineBase):
    def test_clean_home_none_pending(self):
        p = self.mm("status")
        self.assertOk(p)
        self.assertIn("Machine migrations: none pending", p.stdout)

    def test_install_demo_no_becomes_demo_off(self):
        cfg = self.config(vault_path="/nowhere", install_demo="no")
        before = cfg.read_text()
        p = self.mm("status")
        self.assertOk(p)
        self.assertIn("Machine migrations pending:", p.stdout)
        self.assertIn("[0.14.0] %s" % REAL_ID, p.stdout)

        p = self.mm("run")
        self.assertOk(p)
        self.assertIn("applied %s" % REAL_ID, p.stdout)
        self.assertEqual(json.loads(self.choices.read_text()),
                         {"version": 1, "choices": {"demo": "off"}})
        self.assertEqual(cfg.read_text(), before, "install_demo must be left untouched")
        st = self.state()
        self.assertEqual(st["version"], 1)
        self.assertIn(REAL_ID, st["applied"])
        self.assertEqual(st["last_release"], st["applied"][REAL_ID]["release"])
        self.assertIn("at", st["applied"][REAL_ID])

        p = self.mm("status")
        self.assertIn("none pending", p.stdout)
        # No temp files from the atomic write.
        self.assertEqual([f.name for f in self.gt.iterdir() if f.name.endswith(".tmp")], [])

    def test_install_choices_bytes_and_mode_match_record_choice(self):
        # R1 (0.15.0): the migration wrote unsorted keys; record-choice sorted ones and
        # a different mode. One writer's output must equal the other's.
        self.config(install_demo="no")
        self.assertOk(self.mm("run"))
        doc = {"version": 1, "choices": {"demo": "off"}}
        self.assertEqual(self.choices.read_text(),
                         json.dumps(doc, indent=2, sort_keys=True) + "\n")
        self.assertEqual(oct(self.choices.stat().st_mode & 0o777), oct(0o600))
        via = self.tmp / "other"
        (via / ".claude").mkdir(parents=True)
        self.assertOk(self.py(SCRIPTS / "gt_components.py", "record-choice", via, "demo", "off"))
        other = via / ".claude" / "golden-thread" / "install-choices.json"
        self.assertEqual(other.read_bytes(), self.choices.read_bytes())
        self.assertEqual(other.stat().st_mode & 0o777, self.choices.stat().st_mode & 0o777)

    def test_install_demo_yes_becomes_on(self):
        self.config(install_demo="yes")
        self.assertOk(self.mm("run"))
        self.assertEqual(json.loads(self.choices.read_text())["choices"], {"demo": "on"})

    def test_merges_into_existing_choices_and_backs_up(self):
        self.config(install_demo="no")
        self.choices.write_text(json.dumps({"version": 1, "extra": "x",
                                            "choices": {"watch": "on"}}))
        self.assertOk(self.mm("run"))
        data = json.loads(self.choices.read_text())
        self.assertEqual(data["choices"], {"watch": "on", "demo": "off"})
        self.assertEqual(data["extra"], "x")
        backups = list((self.gt / "backups").glob("machine-%s-*" % REAL_ID))
        self.assertEqual(len(backups), 1)
        copy = backups[0] / ".claude" / "golden-thread" / "install-choices.json"
        self.assertEqual(json.loads(copy.read_text())["choices"], {"watch": "on"})

    def test_existing_demo_choice_not_needed(self):
        self.config(install_demo="no")
        self.choices.write_text(json.dumps({"version": 1, "choices": {"demo": "on"}}))
        p = self.mm("status")
        self.assertIn("none pending", p.stdout)
        self.assertOk(self.mm("run"))
        self.assertEqual(json.loads(self.choices.read_text())["choices"], {"demo": "on"})

    def test_dry_run_writes_nothing(self):
        self.config(install_demo="no")
        before = self.tree()
        p = self.mm("run", "--dry-run")
        self.assertOk(p)
        self.assertIn("would apply %s" % REAL_ID, p.stdout)
        self.assertEqual(self.tree(), before)

    def test_restored_machine_reapplies(self):
        self.config(install_demo="no")
        self.assertOk(self.mm("run"))
        self.choices.unlink()                     # restored from an older backup
        p = self.mm("status")
        self.assertIn(REAL_ID, p.stdout)
        self.assertIn("re-applying", p.stdout)
        self.assertOk(self.mm("run"))
        self.assertEqual(json.loads(self.choices.read_text())["choices"], {"demo": "off"})

    def test_unreadable_state_is_exit_2(self):
        self.state_path.write_text("{not json")
        self.assertEqual(self.mm("status").returncode, 2)


FARM_ID = "farm-kept-for-upgraders"


class FarmKeptForUpgraders(MachineBase):
    def choices_now(self):
        return json.loads(self.choices.read_text())["choices"] if self.choices.exists() else {}

    def installed(self, version):
        p = self.home / ".claude" / "plugins" / "installed_plugins.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"version": 2, "plugins": {
            "gt@golden-thread-plugin": [{"version": version}]}}))

    def test_an_upgrader_from_0_14_keeps_farm(self):
        p = self.mm("run", "--previous-release", "0.14.0")
        self.assertOk(p)
        self.assertIn("applied %s" % FARM_ID, p.stdout)
        self.assertEqual(self.choices_now(), {"farm": "on"})
        self.assertIn("none pending", self.mm("status", "--previous-release", "0.14.0").stdout)

    def test_the_environment_carries_it_too(self):
        self.assertOk(self.mm("run", env={"GT_PREVIOUS_RELEASE": "0.12.8"}))
        self.assertEqual(self.choices_now(), {"farm": "on"})

    def test_a_fresh_install_gets_the_default(self):
        p = self.mm("run")
        self.assertOk(p)
        self.assertNotIn(FARM_ID, p.stdout)
        self.assertEqual(self.choices_now(), {})

    def test_releases_that_never_had_farm_or_already_moved_it(self):
        for prev in ("0.9.3", "0.15.0", "0.16.2", "not-a-version"):
            with self.subTest(prev=prev):
                p = self.mm("status", "--previous-release", prev)
                self.assertNotIn(FARM_ID, p.stdout)
        self.assertIn(FARM_ID, self.mm("status", "--previous-release", "0.9.4").stdout)

    def test_a_recorded_choice_is_never_overridden(self):
        for state in ("off", "on"):
            with self.subTest(state=state):
                self.choices.write_text(json.dumps({"version": 1, "choices": {"farm": state}}))
                self.assertOk(self.mm("run", "--previous-release", "0.14.0"))
                self.assertEqual(self.choices_now(), {"farm": state})

    def test_merges_and_backs_up(self):
        self.choices.write_text(json.dumps({"version": 1, "choices": {"demo": "off"}}))
        self.assertOk(self.mm("run", "--previous-release", "0.13.0"))
        self.assertEqual(self.choices_now(), {"demo": "off", "farm": "on"})
        self.assertTrue(list((self.gt / "backups").glob("machine-%s-*" % FARM_ID)))

    def test_read_from_the_machine_without_the_flag(self):
        # installed_plugins.json still naming the old gt: the migrator run by hand pre-install
        self.installed("0.14.0")
        self.assertIn(FARM_ID, self.mm("status").stdout)
        # ...naming THIS release (install.sh already rewrote it): last_release decides
        self.installed(GT.name)
        self.assertNotIn(FARM_ID, self.mm("status").stdout)
        self.state_path.write_text(json.dumps({"version": 1, "applied": {},
                                               "last_release": "0.14.0"}))
        p = self.mm("run")
        self.assertOk(p)
        self.assertIn("applied %s" % FARM_ID, p.stdout)
        self.assertEqual(self.state()["last_release"], GT.name)

    def test_settings_off_are_not_module_choices(self):
        self.config(vault_path="/v", watch="off", report_card="off", closeout_check="off")
        self.assertOk(self.mm("run", "--previous-release", "0.14.0"))
        self.assertEqual(self.choices_now(), {"farm": "on"},
                         "a setting switched off was turned into a module choice")


FIXTURE = textwrap.dedent('''
    import os
    from pathlib import Path

    def marker(ctx, name):
        return ctx.home / ".claude" / "fx" / name

    def mk(name, fail_unless=None, raise_needed=False, outside=False):
        def needed(ctx):
            if raise_needed:
                raise RuntimeError("cannot look")
            return not marker(ctx, name).exists()
        def apply(ctx):
            if fail_unless and not (ctx.home / ".claude" / fail_unless).exists():
                raise RuntimeError("precondition missing")
            if outside:
                ctx.write_json(ctx.home / "outside.json", {})
            order = ctx.home / ".claude" / "fx" / "order"
            order.parent.mkdir(parents=True, exist_ok=True)
            with open(order, "a") as fh:
                fh.write(name + "\\n")
            marker(ctx, name).write_text("x")
        return {"id": name, "introduced": "0.14.0", "description": "fixture " + name,
                "needed": needed, "apply": apply}

    MODE = os.environ.get("FX_MODE", "chain")
    if MODE == "chain":
        MIGRATIONS = (mk("a"), mk("b", fail_unless="fix"), mk("c"))
    elif MODE == "raise":
        MIGRATIONS = (mk("a"), mk("broken", raise_needed=True))
    elif MODE == "outside":
        MIGRATIONS = (mk("escape", outside=True),)
''')


class FixtureRegistry(MachineBase):
    def setUp(self):
        super().setUp()
        self.fx = self.tmp / "fixture_migrations.py"
        self.fx.write_text(FIXTURE)

    def run_fx(self, *args, mode="chain"):
        return self.mm(*args, env={"GT_MACHINE_MIGRATIONS_MODULE": str(self.fx),
                                   "FX_MODE": mode})

    def order(self):
        p = self.home / ".claude" / "fx" / "order"
        return p.read_text().split() if p.exists() else []

    def test_order_stop_on_failure_and_resume(self):
        p = self.run_fx("status")
        self.assertOk(p)
        lines = [l.split()[1] for l in p.stdout.splitlines()[1:]]
        self.assertEqual(lines, ["a", "b", "c"])

        p = self.run_fx("run")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("applied a", p.stdout)
        self.assertIn("FAILED b: precondition missing", p.stdout)
        self.assertNotIn("applied c", p.stdout)
        self.assertEqual(self.order(), ["a"])
        st = self.state()
        self.assertEqual(sorted(st["applied"]), ["a"])
        self.assertNotIn("last_release", st)

        (self.home / ".claude" / "fix").write_text("")
        p = self.run_fx("run")
        self.assertOk(p)
        self.assertEqual(self.order(), ["a", "b", "c"])
        self.assertNotIn("applied a", p.stdout)
        self.assertEqual(sorted(self.state()["applied"]), ["a", "b", "c"])
        self.assertIn("last_release", self.state())
        self.assertEqual([f.name for f in self.gt.iterdir() if f.name.endswith(".tmp")], [])
        self.assertIn("none pending", self.run_fx("status").stdout)

    def test_recorded_but_needed_again_reapplies(self):
        (self.home / ".claude" / "fix").write_text("")
        self.assertOk(self.run_fx("run"))
        (self.home / ".claude" / "fx" / "b").unlink()
        p = self.run_fx("status")
        self.assertIn("] b ", p.stdout)
        self.assertIn("re-applying", p.stdout)
        p = self.run_fx("run")
        self.assertOk(p)
        self.assertIn("applied b", p.stdout)
        self.assertEqual(self.order(), ["a", "b", "c", "b"])

    def test_needed_that_raises_is_exit_2(self):
        p = self.run_fx("status", mode="raise")
        self.assertEqual(p.returncode, 2)
        self.assertIn("broken", p.stderr)
        p = self.run_fx("run", mode="raise")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.order(), [], "nothing applies when the plan cannot be made")

    def test_write_outside_claude_refused(self):
        p = self.run_fx("run", mode="outside")
        self.assertEqual(p.returncode, 1)
        self.assertIn("FAILED escape", p.stdout)
        self.assertFalse((self.home / "outside.json").exists())
