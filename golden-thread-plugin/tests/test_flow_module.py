"""gt-flow as a gt 0.15.0 module: the flow visualizer (design-addons card #17, §3.1).

Contracts pinned here (owner decision 2026-09-14):
  * module.json is valid (gt_components.validate_module, dev/plugins.py module-check and
    manifest-check), agrees with plugin.json and its directory, lists exactly what it
    ships, owns no hooks or settings, replaces nothing in core, and ships a demo act;
  * the skill carries triggers that collide with no other gt or module skill, and states
    the always-redact-before-sharing rule;
  * `gt_flow.py render` writes ONE self-contained HTML file with no network resource,
    whose data-count equals the number of marks visible at load; every (filtered) event
    has a mark, but task.* marks start hidden and unchecked unless `--tasks` is given;
  * `--redact` output contains no fixture project name, path, domain, session or note;
  * an unknown event `v` is refused with a clear error; missing or empty events exit 2
    with directions; an --out inside the vault is refused; the vault is byte-identical
    after a render;
  * install.sh installs the module by default and `--without flow` removes it.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from _harness import Sandbox, GT, WIKI, REPO, PYTHON, SCRIPTS, load_module, latest_version_dir

FLOW = latest_version_dir(REPO / "golden-thread-flow")
SCRIPT = FLOW / "scripts" / "gt_flow.py"
SKILL = FLOW / "skills" / "gt-flow" / "SKILL.md"
INSTALL = REPO / "install.sh"
MARKET = "golden-thread-plugin"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")

# Names chosen so none of them is a word the page template uses anyway.
PROJECTS = ("zephyr-orchard", "quokka-ledger", "marmot-lab/tundra-probe")
SECRET_PATHS = (
    "Projects/zephyr-orchard/memory/pollen-cipher.md",
    "Projects/zephyr-orchard/research.md",
    "Knowledge/pollen-cipher-method.md",
    "global-memory/zephyr-pollen-rule.md",
    "Projects/golden-thread/core-rules/core_zephyr_guard.md",
    "Projects/quokka-ledger/decisions.md",
    "Sources/quokka-bank-export.md",
    "Projects/marmot-lab/tundra-probe/design.md",
)
DOMAIN = "wombat-holdings"
NOTE = "moved the pollen cipher after the kestrel review"


def ev(ts, kind, item, project, frm=None, to=None, lf=None, lt=None, note=None,
       session="sess-kestrel-01", actor="claude"):
    e = {"v": 1, "ts": ts, "session": session, "actor": actor, "kind": kind, "item": item,
         "from": frm, "to": to, "level_from": lf, "level_to": lt, "project": project}
    if note is not None:
        e["note"] = note
    return e


def fixture_events():
    zo, ql, mt = PROJECTS
    return [
        ev("2026-08-01T09:00:00-05:00", "capture", SECRET_PATHS[0], zo, to=SECRET_PATHS[0], lt=2),
        ev("2026-08-02T10:00:00-05:00", "file", SECRET_PATHS[1], zo, frm=SECRET_PATHS[0],
           to=SECRET_PATHS[1], lf=2, lt=3, note=NOTE),
        ev("2026-08-05T11:30:00-05:00", "promote", SECRET_PATHS[2], zo, frm=SECRET_PATHS[1],
           to=SECRET_PATHS[2], lf=3, lt=4),
        ev("2026-08-09T08:15:00+00:00", "promote", SECRET_PATHS[3], zo, frm=SECRET_PATHS[2],
           to=SECRET_PATHS[3], lf=4, lt=5, session="sess-kestrel-02"),
        ev("2026-08-12T16:00:00-05:00", "promote", SECRET_PATHS[4], zo, frm=SECRET_PATHS[3],
           to=SECRET_PATHS[4], lf=5),
        ev("2026-08-03T12:00:00-05:00", "adr", SECRET_PATHS[5], ql, to=SECRET_PATHS[5], lt=3,
           actor="user"),
        ev("2026-08-04T12:00:00-05:00", "ingest", SECRET_PATHS[6], ql, to=SECRET_PATHS[6]),
        ev("2026-08-06T12:00:00-05:00", "task.open", "quokka-ledger#7", ql),
        ev("2026-08-10T12:00:00-05:00", "task.done", "quokka-ledger#7", ql, actor="cron"),
        ev("2026-08-07T07:00:00-05:00", "create", SECRET_PATHS[7], mt, to=SECRET_PATHS[7], lt=3,
           actor="backfill"),
        ev("2026-08-11T07:00:00-05:00", "retire", SECRET_PATHS[7], mt, frm=SECRET_PATHS[7], lf=3),
        ev("2026-08-13T07:00:00-05:00", "announce", "Knowledge/pollen-cipher-method.md", None),
    ]


TASK_KINDS = ("task.open", "task.done")


def not_task(events):
    return [e for e in events if e["kind"] not in TASK_KINDS]


def write_vault(root, events=None, raw=None):
    v = root / "vault"
    (v / "Projects" / "golden-thread").mkdir(parents=True)
    for p, stage in zip(PROJECTS, ("active", "paused", "active")):
        d = v / "Projects" / Path(p)
        d.mkdir(parents=True, exist_ok=True)
        (d / "README.md").write_text("---\ntitle: X\nstage: %s   # comment\ndomain: %s\n---\n# X\n"
                                     % (stage, DOMAIN), encoding="utf-8")
    target = v / "Projects" / "golden-thread" / "events.jsonl"
    if raw is not None:
        target.write_text(raw, encoding="utf-8")
    elif events is not None:
        target.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return v


def tree_digest(root):
    h = hashlib.sha256()
    for dirpath, dirs, files in os.walk(root):
        dirs.sort()
        rel = os.path.relpath(dirpath, root)
        h.update(("D " + rel + "\n").encode())
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            h.update(("F %s/%s %s\n" % (rel, f, digest)).encode())
    return h.hexdigest()


class FlowModuleJson(unittest.TestCase):
    def setUp(self):
        self.mod = json.loads((FLOW / "module.json").read_text(encoding="utf-8"))
        self.plugin = json.loads((FLOW / ".claude-plugin" / "plugin.json").read_text())

    def test_validates_with_the_shipped_validator(self):
        sys.path.insert(0, str(SCRIPTS))
        try:
            gc = load_module(SCRIPTS / "gt_components.py", "gtc_flow_module")
        finally:
            sys.path.pop(0)
        data, reasons = gc.validate_module(str(FLOW))
        self.assertIsNotNone(data, "no module.json")
        self.assertEqual(reasons, [])

    def test_dev_module_and_manifest_check(self):
        for cmd in ("module-check", "manifest-check"):
            p = subprocess.run([PYTHON, str(REPO / "dev" / "plugins.py"), cmd, str(FLOW)],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, cmd + "\n" + p.stdout + p.stderr)

    def test_contract(self):
        m = self.mod
        self.assertEqual((m["schema"], m["name"], m["plugin"], m["version"]),
                         (1, "flow", "gt-flow", "0.15.0"))
        self.assertEqual(m["version"], FLOW.name)
        self.assertEqual((self.plugin["name"], self.plugin["version"]), ("gt-flow", m["version"]))
        self.assertEqual(m["requires_gt"], ">=0.15.0,<0.16.0")
        self.assertEqual(m["default"], "on")
        self.assertEqual(m["skills"], ["gt-flow"])
        self.assertEqual(m["scripts"], ["gt_flow.py"])
        for key in ("hooks", "hookdir_scripts", "settings", "replaces_core"):
            self.assertEqual(m.get(key, []), [], key)
        core_pj = json.loads((GT / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(self.plugin["author"], core_pj["author"])

    def test_lists_exactly_what_it_ships(self):
        for group in ("skills", "scripts", "templates"):
            d = FLOW / group
            shipped = sorted(p.name for p in d.iterdir()
                             if p.name != "__pycache__" and not p.name.startswith(".")) \
                if d.is_dir() else []
            self.assertEqual(sorted(self.mod.get(group, [])), shipped, group)

    def test_demo_act_is_well_formed_and_uses_the_module_script(self):
        self.assertEqual(self.mod["demo"], "demo/act.md")
        act = (FLOW / "demo" / "act.md").read_text()
        self.assertRegex(act, r"(?m)^## (?!Act )\S")
        for key in ("narration:", "do:", "point:"):
            self.assertRegex(act, r"(?m)^%s " % key)
        self.assertIn("the gt-flow skill", act)
        self.assertIn("--redact", act)
        names = re.findall(r"<module:flow>/([A-Za-z0-9_.-]+)", act)
        self.assertTrue(names)
        for name in names:
            self.assertTrue((FLOW / "scripts" / name).is_file(), name)


class FlowSkill(unittest.TestCase):
    def setUp(self):
        self.text = SKILL.read_text(encoding="utf-8")

    def test_frontmatter_and_rules(self):
        self.assertRegex(self.text, r"\A---\nname: gt-flow\ndescription: ")
        self.assertIn("--redact", self.text)
        self.assertRegex(self.text, r"\*\*always add `--redact`\*\*")
        self.assertIn("backfill --vault <vault> --dry-run", self.text)
        self.assertIn("--vault", self.text)

    def test_skill_lint_finds_no_collision_across_the_release(self):
        roots = [GT, WIKI, FLOW]
        for name in ("golden-thread-demo", "golden-thread-watch", "golden-thread-farm",
                     "golden-thread-report-card"):
            try:
                roots.append(latest_version_dir(REPO / name))
            except (RuntimeError, OSError):
                pass
        p = subprocess.run([PYTHON, str(SCRIPTS / "skill_lint.py"), *map(str, roots)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        tail = p.stdout.split("No advertised triggers", 1)
        self.assertFalse(len(tail) > 1 and re.search(r"^\s+gt-flow$", tail[1], re.M),
                         "gt-flow advertises no triggers, so rule 2 cannot check it")


class FlowRender(Sandbox):
    def setUp(self):
        super().setUp()
        self.out = self.tmp / "out"
        self.out.mkdir()

    def render(self, vault, *args, out=None):
        target = out if out is not None else self.out / "flow.html"
        return self.py(SCRIPT, "render", "--vault", vault, "--out", target, *args,
                       cwd=str(self.tmp)), target

    def marks(self, page):
        return len(re.findall(r'<g class="mark ', page))

    def visible_marks(self, page):
        return len(re.findall(r'<g class="mark fam-[a-z]+"', page))

    def chart_count(self, page):
        m = re.search(r'<svg id="chart"[^>]*\bdata-count="(\d+)"', page)
        self.assertIsNotNone(m, "no data-count on the chart")
        return int(m.group(1))

    def test_renders_one_self_contained_file_with_every_event(self):
        events = fixture_events()
        vault = write_vault(self.tmp, events)
        before = tree_digest(vault)
        p, target = self.render(vault)
        self.assertOk(p)
        self.assertEqual(os.listdir(self.out), ["flow.html"], "exactly one file is written")
        page = target.read_text(encoding="utf-8")
        shown = len(not_task(events))
        self.assertLess(shown, len(events), "the fixture must carry task events")
        self.assertEqual(self.chart_count(page), shown)
        self.assertEqual(self.visible_marks(page), shown)
        self.assertEqual(self.marks(page), len(events))
        data = json.loads(re.search(r'<script type="application/json" id="flow-data">(.*?)'
                                    r'</script>', page, re.S).group(1))
        self.assertEqual(len(data["events"]), len(events))
        self.assertNotRegex(page, r"(?i)<script[^>]*\bsrc\s*=")
        self.assertNotRegex(page, r"(?i)<link[^>]*\bhref\s*=")
        self.assertNotRegex(page, r"(?i)url\(\s*['\"]?\s*(https?:)?//")
        self.assertNotRegex(page, r"(?i)@import")
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertRegex(page, r"Generated \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
        self.assertIn('data-count="%d">%d</span> of %d events shown<span id="taskhint">'
                      ' (task events hidden — toggle in Kinds)</span>'
                      % (shown, shown, len(events)), page)
        self.assertIn("prefers-color-scheme: dark", page)
        self.assertIn('tabindex="0"', page)
        self.assertIn("overflow-x:auto", page)
        # unredacted: real names are there, and levels climb with arrows
        self.assertIn(PROJECTS[0], page)
        # the thread: capture->file->promote->promote->promote (4 links), create->retire (1)
        self.assertEqual(len(re.findall(r'class="arrow"', page)), 5)
        by_item = {e["to"]: e for e in data["events"] if e["to"]}
        self.assertEqual(by_item[SECRET_PATHS[4]]["level_to"], 6, "core-rules/ shows as Core")
        self.assertEqual(by_item[SECRET_PATHS[6]]["level_to"], None)
        self.assertEqual(tree_digest(vault), before, "render wrote into the vault")

    def test_redact_hides_every_name(self):
        events = fixture_events()
        vault = write_vault(self.tmp, events)
        before = tree_digest(vault)
        p, target = self.render(vault, "--redact")
        self.assertOk(p)
        page = target.read_text(encoding="utf-8")
        self.assertEqual(self.chart_count(page), len(not_task(events)))
        needles = set(PROJECTS) | set(SECRET_PATHS) | {DOMAIN, NOTE, "quokka-ledger#7",
                                                       "sess-kestrel-01", str(vault)}
        needles |= {"zephyr", "quokka", "marmot", "tundra", "pollen", "kestrel", "wombat"}
        for n in sorted(needles):
            self.assertNotIn(n, page, "redacted page contains %r" % n)
        self.assertNotIn(str(vault), p.stdout.replace(str(target), ""))
        self.assertRegex(page, r"\bp-[0-9a-f]{4,}\b")
        self.assertRegex(page, r"\bf-[0-9a-f]{4,}\b")
        data = json.loads(re.search(r'id="flow-data">(.*?)</script>', page, re.S).group(1))
        self.assertTrue(data["redacted"])
        self.assertEqual({e["kind"] for e in data["events"]}, {e["kind"] for e in events})
        self.assertEqual(sorted(e["level_to"] for e in data["events"] if e["level_to"]),
                         [2, 3, 3, 3, 4, 4, 5, 6])   # announce: 4 inferred from Knowledge/
        # stable within one file: the item a promote arrived at is the next one's source
        order = sorted(data["events"], key=lambda e: e["ts"])
        promotes = [e for e in data["events"] if e["kind"] == "promote"]
        self.assertTrue(any(a["to"] == b["from"] for a in promotes for b in promotes), order)
        # a fresh salt per render: two renders do not share hashes
        p2, target2 = self.render(vault, "--redact", out=self.out / "flow2.html")
        self.assertOk(p2)
        h1 = set(re.findall(r"\bf-[0-9a-f]{4,}\b", page))
        h2 = set(re.findall(r"\bf-[0-9a-f]{4,}\b", target2.read_text(encoding="utf-8")))
        self.assertFalse(h1 & h2, "hashes repeat across renders: the salt is not per render")
        self.assertEqual(tree_digest(vault), before)

    def test_project_and_since_filters(self):
        events = fixture_events()
        vault = write_vault(self.tmp, events)
        p, target = self.render(vault, "--project", PROJECTS[0])
        self.assertOk(p)
        page = target.read_text(encoding="utf-8")
        want = sum(1 for e in not_task(events) if e["project"] == PROJECTS[0])
        self.assertEqual((self.chart_count(page), self.visible_marks(page)), (want, want))
        self.assertNotIn(PROJECTS[1], re.search(r'<svg id="chart".*?</svg>', page, re.S).group(0))
        # a parent slug includes its sub-projects; filters combine
        p, target = self.render(vault, "--project", "marmot-lab", "--project", PROJECTS[1],
                                "--since", "2026-08-06")
        self.assertOk(p)
        chosen = [e for e in events if e["project"] in (PROJECTS[1], PROJECTS[2])
                  and e["ts"][:10] >= "2026-08-06"]
        page = target.read_text(encoding="utf-8")
        self.assertEqual(self.marks(page), len(chosen))
        self.assertEqual(self.chart_count(page), len(not_task(chosen)))
        self.assertEqual(self.visible_marks(page), len(not_task(chosen)))
        p, _ = self.render(vault, "--project", "no-such-project")
        self.assertEqual(p.returncode, 3, p.stderr)

    def test_task_events_hidden_by_default_and_tasks_flag_shows_them(self):
        events = fixture_events()
        vault = write_vault(self.tmp, events)
        tasks = len(events) - len(not_task(events))
        p, target = self.render(vault)
        self.assertOk(p)
        self.assertIn("%d task event(s) hidden" % tasks, p.stdout)
        page = target.read_text(encoding="utf-8")
        for k in TASK_KINDS:
            box = re.search(r'<input type="checkbox" name="kind" value="%s"[^>]*>'
                            % re.escape(k), page).group(0)
            self.assertNotIn("checked", box, k)
        self.assertEqual(len(re.findall(r'<g class="mark fam-task off" tabindex="-1"', page)),
                             tasks)
        self.assertRegex(page, r'<input type="checkbox" name="kind" value="promote" checked>')
        data = json.loads(re.search(r'id="flow-data">(.*?)</script>', page, re.S).group(1))
        self.assertEqual(len(data["events"]), len(events), "task events stay in the data")
        # count math: visible + hidden == all, header and chart agree
        self.assertEqual(self.chart_count(page) + tasks, len(events))
        self.assertEqual(self.visible_marks(page) + tasks, self.marks(page))
        self.assertIn('<span id="shown" data-count="%d">' % self.chart_count(page), page)

        p, target = self.render(vault, "--tasks", out=self.out / "tasks.html")
        self.assertOk(p)
        self.assertNotIn("hidden", p.stdout)
        page = target.read_text(encoding="utf-8")
        self.assertEqual(self.chart_count(page), len(events))
        self.assertEqual(self.visible_marks(page), len(events))
        self.assertNotIn(" off", re.search(r'<svg id="chart".*?</svg>', page, re.S).group(0))
        self.assertRegex(page, r'value="task\.open" checked>')
        self.assertIn('data-count="%d">%d</span> of %d events shown<span id="taskhint" hidden>'
                      % ((len(events),) * 3), page)

    def test_unknown_v_is_refused_clearly(self):
        events = fixture_events()
        events[3]["v"] = 2
        vault = write_vault(self.tmp, events)
        p, target = self.render(vault)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("unknown event schema version v=2", p.stderr)
        self.assertIn(":4:", p.stderr)
        self.assertFalse(target.exists())

    def test_invalid_event_is_refused(self):
        events = fixture_events()
        events[0]["kind"] = "teleport"
        vault = write_vault(self.tmp, events)
        p, target = self.render(vault)
        self.assertEqual(p.returncode, 1)
        self.assertIn("invalid event", p.stderr)
        self.assertFalse(target.exists())

    def test_missing_or_empty_events_exit_2_with_directions(self):
        vault = write_vault(self.tmp)
        before = tree_digest(vault)
        for state in ("missing", "empty"):
            if state == "empty":
                (vault / "Projects" / "golden-thread" / "events.jsonl").write_text("\n\n")
                before = tree_digest(vault)
            p, target = self.render(vault)
            self.assertEqual(p.returncode, 2, state + p.stdout + p.stderr)
            self.assertIn("no events yet", p.stderr)
            self.assertIn("backfill --vault", p.stderr)
            self.assertIn("--dry-run", p.stderr)
            self.assertFalse(target.exists(), "an empty page was written")
            self.assertEqual(os.listdir(self.out), [])
            self.assertEqual(tree_digest(vault), before)

    def test_redaction_self_check_refuses_a_leak(self):
        # If a code path ever bypassed the hash, the render must refuse, not write.
        events = fixture_events()
        vault = write_vault(self.tmp, events)
        sys.dont_write_bytecode = True
        g = load_module(SCRIPT, "gt_flow_for_selfcheck")

        class Leaky(g.Namer):
            def __call__(self, prefix, value):
                return value

        real = g.Namer
        g.Namer = lambda redact: Leaky(False)
        try:
            with self.assertRaises(g.Refused) as cm:
                g.build(g.read_events(str(vault)), len(events), str(vault), True,
                        g.datetime.now().astimezone(), "")
        finally:
            g.Namer = real
        self.assertIn("redaction self-check failed", str(cm.exception))
        self.assertTrue(g.build(g.read_events(str(vault)), len(events), str(vault), True,
                                g.datetime.now().astimezone(), ""))

    def test_out_inside_the_vault_is_refused_and_default_stays_out(self):
        events = fixture_events()
        vault = write_vault(self.tmp, events)
        before = tree_digest(vault)
        p, _ = self.render(vault, out=vault / "flow.html")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("inside the vault", p.stderr)
        # no --out, run from inside the vault: the file goes to a temp dir, not the vault
        q = self.py(SCRIPT, "render", "--vault", vault, cwd=str(vault / "Projects"))
        self.assertOk(q)
        written = re.search(r"wrote (.+?\.html) ", q.stdout).group(1)
        self.assertTrue(os.path.isfile(written))
        self.assertFalse(os.path.realpath(written).startswith(os.path.realpath(vault)))
        shutil.rmtree(os.path.dirname(written), ignore_errors=True)
        self.assertEqual(tree_digest(vault), before)


class FlowInstall(Sandbox):
    """install.sh against a throwaway HOME and a copy of the source tree."""

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "src" / MARKET
        self.repo.mkdir(parents=True)
        shutil.copy2(INSTALL, self.repo / "install.sh")
        shutil.copytree(GT, self.repo / "golden-thread" / GT.name, ignore=IGNORE)
        shutil.copytree(WIKI, self.repo / "golden-thread-wiki" / WIKI.name, ignore=IGNORE)
        shutil.copytree(FLOW, self.repo / "golden-thread-flow" / FLOW.name, ignore=IGNORE)
        gt_src = self.repo / "golden-thread" / GT.name
        self.assertOk(self.py(gt_src / "scripts" / "gt_components.py", "manifest", gt_src))
        self.claude = self.home / ".claude"

    def install(self, *args):
        p = self.sh(self.repo / "install.sh", *args, "--no-vault", timeout=300)
        self.assertOk(p, "install.sh failed")
        return p

    def jload(self, rel, default=None):
        p = self.claude / rel
        return json.loads(p.read_text()) if p.exists() else default

    def test_on_by_default_then_without_removes_it(self):
        key = "gt-flow@%s" % MARKET
        cache = self.claude / "plugins" / "cache" / MARKET / "gt-flow"
        p = self.install()
        self.assertTrue(list(cache.glob("*/skills/gt-flow/SKILL.md")), "skill not installed")
        self.assertTrue(list(cache.glob("*/scripts/gt_flow.py")), "script not installed")
        self.assertIn(key, self.jload("plugins/installed_plugins.json")["plugins"])
        self.assertIs(self.jload("settings.json")["enabledPlugins"].get(key), True)

        self.install("--without", "flow")
        self.assertFalse(cache.exists(), "module cache left behind")
        self.assertFalse(list((self.claude / "plugins").rglob("skills/gt-flow")),
                         "a plugin still carries the skill")
        self.assertNotIn(key, (self.jload("settings.json", {}) or {}).get("enabledPlugins", {}))
        choices = (self.jload("golden-thread/install-choices.json", {}) or {}).get("choices", {})
        self.assertEqual(choices.get("flow"), "off")
        hooks = json.dumps((self.jload("settings.json", {}) or {}).get("hooks", {}))
        self.assertNotIn("gt_flow", hooks)


if __name__ == "__main__":
    unittest.main()
