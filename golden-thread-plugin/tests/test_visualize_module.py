"""gt-visualize as a module: a repository drawn as a 3D code city with three.js.

Feature request 2026-09-29-gt-visualize-3d-codebase-view. Contracts pinned here:
  * module.json is valid, agrees with plugin.json and its directory, lists exactly what it
    ships, owns no hooks or settings, and ships a demo act;
  * the vendored three.js bundle is the one VENDOR.json and the script's pinned hash name,
    and a tampered bundle is refused rather than inlined;
  * the skill carries triggers and the always-redact-before-sharing rule;
  * `render` writes ONE HTML file that loads nothing from the network;
  * the embedded data lists exactly `git ls-files --cached --others --exclude-standard`,
    with line counts, sizes, languages (gt's filetype definitions) and churn counts that
    match independent measurements;
  * every directory's rect contains its children's, and no two siblings overlap;
  * `--redact` leaves no fixture name in the page, and one salt hashes identically twice;
  * an --out inside the vault is refused and nothing is written;
  * outside git, churn is off and the output line says so;
  * install.sh installs the module by default and `--without visualize` removes it;
  * (2026-09-30-gt-visualize-guided-walkthrough) `story scene|part|questions` read only a valid
    story, and every expected answer is text from it; `tour-state` round-trips under a
    sandbox vault, --dry-run writes nothing, and a file another live session claims is
    refused; the four walkthrough skills exist and name their prediction pause.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import unittest
from pathlib import Path

from _harness import Sandbox, GT, WIKI, REPO, PYTHON, SCRIPTS, TOOLS, GIT_ID, \
    latest_version_dir, gt_requires_range, load_module

VIS = latest_version_dir(REPO / "golden-thread-visualize")
SCRIPT = VIS / "scripts" / "gt_visualize.py"
SKILL = VIS / "skills" / "gt-visualize" / "SKILL.md"
BUNDLE = VIS / "scripts" / "vendor" / "three-bundle.min.js"
INSTALL = REPO / "install.sh"
MARKET = "golden-thread-plugin"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
DAY = 86400

# Names chosen so none of them is a word the page, three.js or a language label uses.
FIXTURE = {
    "quokka-core/zephyr_engine.py": "import os\n\n\ndef zephyr():\n    return 1\n",
    "quokka-core/kestrel_view.js": "function kestrel() {\n  return 2;\n}\n",
    "quokka-core/marmot/tundra_probe.go": "package marmot\n\nfunc Tundra() {}\n",
    "wombat-notes/pollen-cipher.md": "# Pollen\n\none\ntwo\nthree",   # no final newline
    "wombat-notes/settings-orchard.yaml": "orchard: true\n",
    "wombat-notes/deep/deeper/axolotl-ledger.sql": "SELECT 1;\n",
    "Makefile": "all:\n\techo hi\n",
    "narwhal.toml": "[narwhal]\nx = 1\n",
}
BINARY = "wombat-notes/ibex-sprite.png"
IGNORED = "gecko-build/output.log"


def git(root, *args, date=None):
    env = dict(os.environ, **GIT_ID)
    for k in [k for k in env if k.startswith("GIT_") and k not in GIT_ID]:
        del env[k]
    if date is not None:
        stamp = "@%d +0000" % date
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = stamp
    return subprocess.run(["git", "-C", str(root), *args], env=env, capture_output=True,
                          text=True, check=True)


def make_repo(base, with_git=True):
    root = base / "ocelot-repo"
    for rel, text in FIXTURE.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (root / BINARY).write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + bytes(range(256)))
    (root / IGNORED).parent.mkdir(parents=True, exist_ok=True)
    (root / IGNORED).write_text("noise\n", encoding="utf-8")
    (root / ".gitignore").write_text("gecko-build/\n", encoding="utf-8")
    if not with_git:
        return root
    now = int(time.time())
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "old", date=now - 100 * DAY)
    for n in range(3):          # three recent changes to one file, one to another
        with open(root / "quokka-core/zephyr_engine.py", "a", encoding="utf-8") as fh:
            fh.write("# %d\n" % n)
        if n == 0:
            with open(root / "narwhal.toml", "a", encoding="utf-8") as fh:
                fh.write("y = 2\n")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "recent %d" % n, date=now - (5 - n) * DAY)
    return root


def data_of(page):
    m = re.search(r'<script type="application/json" id="gtv-data">(.*?)</script>', page, re.S)
    assert m, "no embedded data"
    return json.loads(m.group(1).replace("<\\/", "</"))


def without_bundle(page):
    return page.replace(BUNDLE.read_text(encoding="utf-8").replace("</script", "<\\/script"), "")


class VisualizeModule(unittest.TestCase):
    def setUp(self):
        self.module = json.loads((VIS / "module.json").read_text(encoding="utf-8"))
        self.plugin = json.loads((VIS / ".claude-plugin" / "plugin.json").read_text("utf-8"))

    def test_module_is_valid_and_agrees_with_plugin(self):
        p = subprocess.run([PYTHON, str(REPO / "dev" / "plugins.py"), "module-check", str(VIS),
                            "--gt", GT.name], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        m = self.module
        self.assertEqual((m["name"], m["plugin"], m["version"]),
                         ("visualize", "gt-visualize", VIS.name))
        self.assertEqual((self.plugin["name"], self.plugin["version"]),
                         ("gt-visualize", m["version"]))
        self.assertEqual(m["skills"], ["gt-visualize", "tour", "whatis", "trace", "explain-back"])
        self.assertEqual(m["scripts"], ["gt_visualize.py"])
        self.assertEqual((m["hooks"], m["hookdir_scripts"], m["replaces_core"]), ([], [], []))
        self.assertEqual([s["key"] for s in m["settings"]],
                         ["visualize_publish", "visualize_publish_visibility"])
        self.assertTrue((VIS / m["demo"]).is_file())
        self.assertIn("the gt-visualize skill", (VIS / m["demo"]).read_text("utf-8"))
        # gt-visualize keeps its own version train (like gt-usage); what matters is that its
        # requires_gt admits the gt in this tree, which module-check --gt above proves.
        self.assertTrue(m["requires_gt"].startswith(">="), m["requires_gt"])

    def test_manifest_matches_the_tree(self):
        p = subprocess.run([PYTHON, str(REPO / "dev" / "plugins.py"), "manifest-check",
                            str(VIS)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_vendored_three_is_the_recorded_one(self):
        digest = hashlib.sha256(BUNDLE.read_bytes()).hexdigest()
        vendor = json.loads((VIS / "scripts" / "vendor" / "VENDOR.json").read_text(encoding="utf-8"))
        entry = vendor["three-bundle.min.js"]
        self.assertEqual(entry["sha256"], digest)
        self.assertEqual(load_module(SCRIPT, "gtv_hash").THREE_SHA256, digest)
        self.assertEqual(entry["license"].split()[0], "MIT")
        self.assertTrue((VIS / "scripts" / "vendor" / "THREE-LICENSE").is_file())
        self.assertIn("@license", BUNDLE.read_text(encoding="utf-8")[:2000])

    def test_skill_carries_triggers_and_the_redact_rule(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertRegex(text, r"\A---\nname: gt-visualize\ndescription: ")
        self.assertIn("Use when the user says:", text)
        self.assertRegex(text, r"\*\*always add `--redact`\*\*")

    def test_skill_lint_finds_no_collision_across_the_release(self):
        roots = [GT, WIKI, VIS]
        for name in ("golden-thread-demo", "golden-thread-watch", "golden-thread-farm",
                     "golden-thread-report-card", "golden-thread-flow", "golden-thread-usage"):
            try:
                roots.append(latest_version_dir(REPO / name))
            except (RuntimeError, OSError):
                pass
        p = subprocess.run([PYTHON, str(SCRIPTS / "skill_lint.py"), *map(str, roots)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        tail = p.stdout.split("No advertised triggers", 1)
        self.assertFalse(len(tail) > 1 and re.search(r"^\s+gt-visualize$", tail[1], re.M),
                         "gt-visualize advertises no triggers")


class VisualizeRender(Sandbox):
    def setUp(self):
        super().setUp()
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.vault = self.tmp / "vault"
        (self.vault / "Projects").mkdir(parents=True)

    def render(self, path, *args, out=None):
        target = out if out is not None else self.out / "city.html"
        p = self.py(SCRIPT, "render", path, "--out", target, "--vault", self.vault, *args,
                    cwd=str(self.tmp))
        return p, target

    def test_one_offline_file_whose_data_matches_git(self):
        root = make_repo(self.tmp)
        p, target = self.render(root)
        self.assertOk(p)
        self.assertEqual(os.listdir(self.out), ["city.html"], "exactly one file is written")
        page = target.read_text(encoding="utf-8")
        rest = without_bundle(page)
        self.assertNotEqual(rest, page, "three.js must be inlined")
        self.assertNotRegex(page, r"(?i)<script[^>]*\bsrc\s*=")
        self.assertNotRegex(page, r"(?i)<link[^>]*\bhref\s*=")
        self.assertNotRegex(page, r"(?i)<img[^>]*\bsrc\s*=")
        self.assertNotRegex(rest, r"(?i)url\(|@import|fetch\(|import\(")
        self.assertNotIn("http://", rest)
        self.assertNotIn("https://", rest)
        # The bundle's only URLs are a namespace string and a citation, never a load.
        urls = set(re.findall(r"https?://[^\s\"'`)]+", BUNDLE.read_text(encoding="utf-8")))
        self.assertLessEqual(urls, {"http://www.w3.org/1999/xhtml",
                                    "https://jcgt.org/published/0007/04/01/"})

        d = data_of(page)
        listed = git(root, "ls-files", "--cached", "--others", "--exclude-standard").stdout
        expect = sorted(x for x in listed.splitlines() if x)
        got = sorted(f["p"] for f in d["files"])
        self.assertEqual(got, expect)
        self.assertNotIn(IGNORED, got)
        byp = {f["p"]: f for f in d["files"]}
        for rel in FIXTURE:
            raw = (root / rel).read_bytes()
            lines = raw.count(b"\n") + (1 if raw and not raw.endswith(b"\n") else 0)
            self.assertEqual(byp[rel]["l"], lines, rel)
            self.assertEqual(byp[rel]["b"], len(raw), rel)
        self.assertEqual(byp[BINARY].get("bin"), 1)
        self.assertIsNone(byp[BINARY]["l"])
        self.assertEqual(d["meta"]["files"], len(expect))
        self.assertIn("%d files" % len(expect), p.stdout)

    def test_language_is_gts_filetype_definition(self):
        root = make_repo(self.tmp)
        p, target = self.render(root)
        self.assertOk(p)
        self.assertIn("language from gt filetype definitions", p.stdout)
        lang = load_module(SCRIPTS / "gt_scan_language.py", "gtsl_for_test")
        entries, _ = lang._entries("filetype", None)
        filetypes = {e["match"]: e["lang"] for e in entries}
        d = data_of(target.read_text(encoding="utf-8"))
        known = 0
        for f in d["files"]:
            want = lang.lang_of(f["p"], filetypes)
            if want is not None and not f.get("bin"):
                known += 1
                self.assertEqual(f["g"], want, f["p"])
        self.assertGreaterEqual(known, 5, "the fixture must exercise gt's definitions")
        byp = {f["p"]: f["g"] for f in d["files"]}
        self.assertEqual(byp["narwhal.toml"], "toml", "fallback for what gt does not know")

    def test_churn_counts_match_git_log(self):
        root = make_repo(self.tmp)
        p, target = self.render(root, "--since", "30")
        self.assertOk(p)
        d = data_of(target.read_text(encoding="utf-8"))
        self.assertTrue(d["meta"]["churn"])
        log = git(root, "log", "--since=30.days", "--no-renames", "--format=",
                  "--name-only").stdout.split()
        byp = {f["p"]: f["c"] for f in d["files"]}
        for rel, n in byp.items():
            self.assertEqual(n, log.count(rel), rel)
        self.assertEqual(byp["quokka-core/zephyr_engine.py"], 3)
        self.assertEqual(byp["narwhal.toml"], 1)
        self.assertEqual(byp["wombat-notes/pollen-cipher.md"], 0)

    def test_layout_nests_and_siblings_do_not_overlap(self):
        root = make_repo(self.tmp)
        p, target = self.render(root)
        self.assertOk(p)
        d = data_of(target.read_text(encoding="utf-8"))
        dirs, eps = d["dirs"], 1e-2

        def inside(a, b):
            return (a["x"] >= b["x"] - eps and a["y"] >= b["y"] - eps and
                    a["x"] + a["w"] <= b["x"] + b["w"] + eps and
                    a["y"] + a["h"] <= b["y"] + b["h"] + eps)

        def overlap(a, b):
            return (min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]) > eps and
                    min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]) > eps)

        kids = {}
        for i, dd in enumerate(dirs):
            if dd["u"] is not None:
                self.assertTrue(inside(dd, dirs[dd["u"]]), dd["p"])
                kids.setdefault(dd["u"], []).append(dd)
        for f in d["files"]:
            self.assertTrue(inside(f, dirs[f["d"]]), f["p"])
            kids.setdefault(f["d"], []).append(f)
        for group in kids.values():
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    self.assertFalse(overlap(group[i], group[j]),
                                     "%s overlaps %s" % (group[i]["p"], group[j]["p"]))
        self.assertIn("wombat-notes/deep/deeper", [x["p"] for x in dirs])

    def test_redact_leaves_no_name_and_is_stable_per_salt(self):
        root = make_repo(self.tmp)
        a = self.out / "a.html"
        b = self.out / "b.html"
        self.assertOk(self.render(root, "--redact", "--salt", "s1", out=a)[0])
        self.assertOk(self.render(root, "--redact", "--salt", "s1", out=b)[0])
        page = without_bundle(a.read_text(encoding="utf-8"))
        names = {"ocelot-repo", "gecko-build"}
        for rel in list(FIXTURE) + [BINARY]:
            for seg in rel.split("/"):
                names.update({seg, os.path.splitext(seg)[0]})
        names = {n for n in names if len(n) >= 4}
        for n in sorted(names):
            self.assertNotIn(n, page, n)
        self.assertEqual([f["p"] for f in data_of(a.read_text("utf-8"))["files"]],
                         [f["p"] for f in data_of(b.read_text("utf-8"))["files"]])
        self.assertTrue(data_of(a.read_text("utf-8"))["meta"]["redacted"])

    def test_out_inside_the_vault_is_refused(self):
        root = make_repo(self.tmp)
        before = sorted(str(p) for p in self.vault.rglob("*"))
        p, _ = self.render(root, out=self.vault / "Projects" / "city.html")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("inside the vault", p.stderr)
        self.assertEqual(sorted(str(x) for x in self.vault.rglob("*")), before)

    def test_outside_git_churn_is_off_and_said(self):
        root = make_repo(self.tmp, with_git=False)
        p, target = self.render(root)
        self.assertOk(p)
        self.assertIn("churn off: not a git repository", p.stdout)
        d = data_of(target.read_text(encoding="utf-8"))
        self.assertFalse(d["meta"]["churn"])
        self.assertTrue(all(f["c"] == 0 for f in d["files"]))
        self.assertIn(IGNORED, [f["p"] for f in d["files"]],
                      "without git there is no .gitignore reading")

    def test_exclude_and_empty(self):
        root = make_repo(self.tmp)
        p, target = self.render(root, "--exclude", "wombat-notes/*")
        self.assertOk(p)
        got = [f["p"] for f in data_of(target.read_text("utf-8"))["files"]]
        self.assertFalse([g for g in got if g.startswith("wombat-notes/")])
        p, _ = self.render(root, "--exclude", "*")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertEqual(os.listdir(self.out), ["city.html"])

    def test_max_files_collapses_deep_directories(self):
        root = make_repo(self.tmp)
        p, target = self.render(root, "--max-files", "6")
        self.assertOk(p)
        self.assertIn("collapsed to fit --max-files 6", p.stdout)
        d = data_of(target.read_text("utf-8"))
        self.assertLessEqual(len(d["files"]), 6)
        self.assertEqual(sum(f.get("n", 1) for f in d["files"]), d["meta"]["files"])

    def test_a_tampered_bundle_is_refused(self):
        mod = self.tmp / "mod"
        shutil.copytree(VIS, mod, ignore=IGNORE)
        with open(mod / "scripts" / "vendor" / "three-bundle.min.js", "a", encoding="utf-8") as fh:
            fh.write("\n/* changed */\n")
        root = make_repo(self.tmp)
        p = self.py(mod / "scripts" / "gt_visualize.py", "render", root, "--out",
                    self.out / "t.html", "--vault", self.vault)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("does not match the vendored hash", p.stderr)
        self.assertFalse((self.out / "t.html").exists())


def story(**over):
    s = {
        "title": "How the kestrel pipeline works",
        "subtitle": "A `queue` and a **gate**",
        "intro": ["Two services and a store."],
        "parts": [
            {"id": "api", "label": "Kestrel API", "group": "Edge", "note": "takes requests"},
            {"id": "gate", "label": "Auth gate", "kind": "gate", "group": "Edge"},
            {"id": "cache", "label": "Wombat cache", "kind": "stack", "group": "Edge"},
            {"id": "db", "label": "Quokka store", "kind": "store", "group": "Data", "size": "l"},
            {"id": "queue", "label": "Marmot queue", "kind": "stack", "group": "Data"},
            {"id": "worker", "label": "Ibex worker", "group": "Data"},
            {"id": "user", "label": "Caller", "kind": "actor", "group": "Outside", "at": [-9, 2]},
            {"id": "admin", "label": "Operator", "kind": "actor", "group": "Outside"},
        ],
        "links": [
            {"from": "user", "to": "api", "label": "request"},
            {"from": "api", "to": "gate"},
            {"from": "gate", "to": "db"},
            {"id": "deny", "from": "gate", "to": "user", "label": "refused"},
            {"from": "api", "to": "queue"},
            {"from": "queue", "to": "worker"},
            {"from": "worker", "to": "db"},
        ],
        "scenes": [
            {"title": "The parts", "body": "All of it <script>alert(1)</script>.", "show": "*",
             "caption": "Every part, from above."},
            {"title": "A request", "body": ["It goes in.", "Then it is `checked`."],
             "caption": "A request reaches the gate; a bad one is sent back.",
             "show": ["user", "api", "gate"], "focus": ["gate"],
             "flows": [{"link": "user>api"}, {"link": "deny", "kind": "block"}]},
            {"title": "Work in the background", "body": "Slow work is queued.",
             "caption": "The API queues work; the worker writes the result to the store.",
             "show": ["api", "queue", "worker", "db"], "focus": ["queue", "worker"],
             "flows": [{"link": "api>queue"}, {"link": "queue>worker"}, {"link": "worker>db"}]},
            {"title": "All together", "body": "That is the whole pipeline.",
             "caption": "Back to the whole picture.", "show": "*"},
        ],
    }
    s.update(over)
    return s


class VisualizeExplain(Sandbox):
    def setUp(self):
        super().setUp()
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.vault = self.tmp / "vault"
        (self.vault / "Projects").mkdir(parents=True)

    def write(self, s):
        p = self.tmp / "story.json"
        p.write_text(json.dumps(s), encoding="utf-8")
        return p

    def explain(self, s, *args, out=None):
        target = out if out is not None else self.out / "story.html"
        return self.py(SCRIPT, "explain", self.write(s), "--out", target, "--vault", self.vault,
                       *args), target

    def test_renders_one_offline_walkthrough(self):
        p, target = self.explain(story())
        self.assertOk(p)
        self.assertIn("8 parts, 7 links, 4 scenes", p.stdout)
        self.assertEqual(os.listdir(self.out), ["story.html"])
        page = target.read_text(encoding="utf-8")
        rest = without_bundle(page)
        self.assertNotEqual(rest, page, "three.js must be inlined")
        self.assertNotRegex(page, r"(?i)<script[^>]*\bsrc\s*=|<link[^>]*\bhref\s*=")
        self.assertNotIn("http://", rest)
        self.assertNotIn("https://", rest)
        self.assertEqual(len(re.findall(r'<section class="step" data-i="\d+">', page)), 4)
        self.assertEqual(page.count('<p class="caption"><span>On the stage:</span>'), 4)
        self.assertIn("<title>How the kestrel pipeline works</title>", page)
        self.assertIn("<code>queue</code>", page)
        self.assertIn("<strong>gate</strong>", page)
        self.assertIn("<code>checked</code>", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("prefers-color-scheme:dark", page)
        self.assertIn("prefers-reduced-motion", page)
        m = re.search(r'<script type="application/json" id="gtx-data">(.*?)</script>', page, re.S)
        d = json.loads(m.group(1).replace("<\\/", "</"))
        self.assertEqual([x["id"] for x in d["parts"]],
                         ["api", "gate", "cache", "db", "queue", "worker", "user", "admin"])
        self.assertEqual([x["id"] for x in d["links"]],
                         ["user>api", "api>gate", "gate>db", "deny", "api>queue", "queue>worker",
                          "worker>db"])
        self.assertEqual(d["scenes"][1]["flows"], [{"link": "user>api", "kind": "data"},
                                                   {"link": "deny", "kind": "block"}])
        self.assertEqual(d["groups"], ["Edge", "Data", "Outside"])
        # F5: context never vanishes unless a story asks; F3's floor reaches the page.
        self.assertTrue(all(sc["ghost"] for sc in d["scenes"]))
        self.assertEqual(d["zoomFloor"], 0.55)
        self.assertIn("F1 the first scene is an establishing shot", page)
        self.assertIn("frame(i)", page)

    def test_every_story_rule_is_enforced_and_named(self):
        def bad(mutate):
            s = story()
            mutate(s)
            p, target = self.explain(s)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertFalse(target.exists())
            return p.stderr

        def few_parts(s):
            s["parts"] = s["parts"][:5]
            s["links"] = [l for l in s["links"] if l["from"] in ("api", "gate", "cache", "db", "queue")
                          and l["to"] in ("api", "gate", "cache", "db", "queue")]
            for sc in s["scenes"]:
                sc["flows"] = []
                if sc["show"] != "*":
                    sc["show"] = ["api", "gate", "db"]
                    sc["focus"] = ["api"]

        cases = {
            "S1": lambda s: s["scenes"][0].update(focus=["api"]),
            "S2": lambda s: s["scenes"][3].update(show=["api", "gate", "db"]),
            "S3": lambda s: s.update(scenes=s["scenes"][:1] + s["scenes"][3:]),
            "S4": few_parts,
            "S5": lambda s: s["parts"][0].update(label="A label that is far too long to read"),
            "S6": lambda s: s["scenes"][1].update(focus=[]),
            "S7": lambda s: s["scenes"][1].update(show=["api", "gate"], flows=[]),
            "S8": lambda s: s["scenes"][2]["flows"].append({"link": "user>api"}),
            "S9": lambda s: s["scenes"][2].update(body=["one", "two", "three"]),
            "S10": lambda s: s["scenes"][2].pop("caption"),
        }
        for rid, mutate in cases.items():
            with self.subTest(rule=rid):
                self.assertIn("rule %s:" % rid, bad(mutate))

    def test_focus_must_be_shown(self):
        s = story()
        s["scenes"][1]["focus"] = ["db"]
        p, _ = self.explain(s)
        self.assertEqual(p.returncode, 1)
        self.assertIn("'db' is not shown -- rule S6", p.stderr)

    def test_layout_groups_are_columns_and_at_wins(self):
        p, target = self.explain(story())
        self.assertOk(p)
        page = target.read_text(encoding="utf-8")
        d = json.loads(re.search(r'id="gtx-data">(.*?)</script>', page, re.S).group(1))
        pos = {x["id"]: (x["x"], x["z"]) for x in d["parts"]}
        self.assertEqual(pos["api"][0], pos["gate"][0], "one group, one column")
        self.assertLess(pos["api"][0], pos["db"][0], "columns in order of first appearance")
        self.assertNotEqual(pos["api"][1], pos["gate"][1])
        self.assertEqual(pos["user"], (-9.0, 2.0))

    def test_check_validates_and_writes_nothing(self):
        p, _ = self.explain(story(), "--check")
        self.assertOk(p)
        self.assertIn("ok ", p.stdout)
        self.assertEqual(os.listdir(self.out), [])

    def test_bad_story_is_refused_with_every_reason(self):
        s = story()
        s["parts"].append({"id": "api", "label": "again", "kind": "blimp"})
        s["links"].append({"from": "api", "to": "nowhere"})
        s["scenes"][1]["flows"].append({"link": "no-such-link"})
        s["scenes"][1]["focus"] = ["ghost-part"]
        s["colour"] = "red"
        p, target = self.explain(s)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        for want in ("'api' is used twice", "kind: one of", "'nowhere' is not a part id",
                     "flows[2].link", "'ghost-part' is not a part id",
                     "unknown top-level key 'colour'", "nothing was written"):
            self.assertIn(want, p.stderr)
        self.assertFalse(target.exists())

    def test_unreadable_or_not_json(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{nope", encoding="utf-8")
        p = self.py(SCRIPT, "explain", bad, "--out", self.out / "x.html", "--vault", self.vault)
        self.assertEqual(p.returncode, 1)
        self.assertIn("not valid JSON", p.stderr)

    def test_out_inside_the_vault_is_refused(self):
        p, _ = self.explain(story(), out=self.vault / "Projects" / "story.html")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("inside the vault", p.stderr)
        self.assertEqual(sorted(x.name for x in self.vault.rglob("*")), ["Projects"])


def _story_strings(s):
    """Every string value in a story, however deep."""
    if isinstance(s, str):
        yield s
    elif isinstance(s, dict):
        for v in s.values():
            yield from _story_strings(v)
    elif isinstance(s, list):
        for v in s:
            yield from _story_strings(v)


def _at(s, path):
    """Resolve a `from` path such as `scenes[1].body[0]` against the story."""
    cur = s
    for name, idx in re.findall(r"([A-Za-z_]+)(?:\[(\d+)\])?", path):
        cur = cur[name]
        if idx:
            cur = cur[int(idx)]
    return cur


class VisualizeStory(Sandbox):
    """`story scene|part|questions`: what the walkthrough skills read, and nothing invented."""

    def setUp(self):
        super().setUp()
        self.story = story()
        self.path = self.tmp / "story.json"
        self.path.write_text(json.dumps(self.story), encoding="utf-8")

    def run_story(self, *args, expect=0):
        p = self.py(SCRIPT, "story", *args)
        self.assertEqual(p.returncode, expect, p.stdout + p.stderr)
        return json.loads(p.stdout) if expect == 0 else p

    def test_scene_by_index_and_by_title_agree(self):
        by_index = self.run_story("scene", self.path, "2")
        by_title = self.run_story("scene", self.path, "a REQUEST")
        self.assertEqual(by_index, by_title)
        self.assertEqual((by_index["index"], by_index["of"], by_index["altitude"]), (2, 4, "L1"))
        self.assertEqual([p["id"] for p in by_index["shown"]], ["user", "api", "gate"])
        self.assertEqual(by_index["focus"], ["gate"])
        self.assertEqual(by_index["hops"], [
            {"n": 1, "link": "user>api", "from": "user", "to": "api", "kind": "data",
             "label": "request"},
            {"n": 2, "link": "deny", "from": "gate", "to": "user", "kind": "block",
             "label": "refused"}])
        self.assertEqual(by_index["next"], {"index": 3, "title": "Work in the background"})
        first = self.run_story("scene", self.path, "1")
        self.assertEqual((first["altitude"], first["hops"], first["previous"]), ("L0", [], None))
        self.assertEqual(len(first["shown"]), 8, "scene 1 shows everything")

    def test_a_return_flow_travels_back_along_its_link(self):
        self.story["scenes"][2]["flows"][2] = {"link": "worker>db", "kind": "return"}
        self.path.write_text(json.dumps(self.story), encoding="utf-8")
        hop = self.run_story("scene", self.path, "3")["hops"][2]
        self.assertEqual((hop["from"], hop["to"], hop["kind"]), ("db", "worker", "return"))

    def test_unknown_scene_is_refused_with_the_choices(self):
        for which in ("9", "0", "No such scene"):
            with self.subTest(which=which):
                p = self.run_story("scene", self.path, which, expect=1)
                self.assertIn("no scene", p.stderr)
                self.assertIn("2 A request", p.stderr)
                self.assertEqual(p.stdout, "")

    def test_an_invalid_story_is_never_walked(self):
        self.story["scenes"][0]["focus"] = ["api"]
        self.path.write_text(json.dumps(self.story), encoding="utf-8")
        for args in (("scene", self.path, "1"), ("part", self.path, "api"),
                     ("questions", self.path)):
            with self.subTest(args=args[0]):
                p = self.run_story(*args, expect=1)
                self.assertIn("rule S1", p.stderr)
                self.assertIn("nothing to walk", p.stderr)

    def test_part_altitude_follows_its_kind(self):
        self.story["parts"][5]["kind"] = "file"
        self.path.write_text(json.dumps(self.story), encoding="utf-8")
        want = {"user": ("L2", None), "gate": ("L2", None), "api": ("L2", "L3"),
                "db": ("L2", "L3"), "cache": ("L2", "L3"), "worker": ("L3", None)}
        for pid, (alt, into) in want.items():
            with self.subTest(part=pid):
                d = self.run_story("part", self.path, pid)
                self.assertEqual(d["altitude"], alt)
                self.assertEqual(d["step_into"] and d["step_into"]["altitude"], into)

    def test_part_neighbours_links_and_scenes(self):
        d = self.run_story("part", self.path, "gate")
        self.assertEqual(d["part"]["label"], "Auth gate")
        self.assertEqual(d["group_members"], ["api", "gate", "cache"])
        self.assertEqual(d["neighbours"], {"previous": "api", "next": "cache"})
        self.assertEqual([r["part"] for r in d["links_in"]], ["api"])
        self.assertEqual([r["part"] for r in d["links_out"]], ["db", "user"])
        self.assertEqual([s["index"] for s in d["scenes"]], [1, 2, 4])
        self.assertEqual(d["focused_in"], [2])
        edge = self.run_story("part", self.path, "api")["neighbours"]
        self.assertEqual(edge, {"previous": None, "next": "gate"})
        p = self.run_story("part", self.path, "blimp", expect=1)
        self.assertIn("no part 'blimp'", p.stderr)

    def test_questions_are_3_to_5_and_every_answer_is_the_storys_own_text(self):
        texts = list(_story_strings(self.story))
        for scope in (None, "db", "gate,user"):
            with self.subTest(parts=scope):
                args = ("questions", self.path) + (("--parts", scope) if scope else ())
                qs = self.run_story(*args)["questions"]
                self.assertTrue(3 <= len(qs) <= 5, len(qs))
                self.assertEqual([q["n"] for q in qs], list(range(1, len(qs) + 1)))
                self.assertTrue({"behaviour", "change impact", "rationale"} <=
                                {q["seam"] for q in qs})
                for q in qs:
                    self.assertTrue(q["question"].strip())
                    self.assertEqual(len(q["expected"]), len(q["from"]))
                    for exp, src in zip(q["expected"], q["from"]):
                        self.assertTrue(any(exp in t for t in texts),
                                        "%r is not text from the story" % exp)
                        got = _at(self.story, src)
                        got = got if isinstance(got, str) else "\n".join(got)
                        self.assertIn(exp, got, "%s does not hold %r" % (src, exp))

    def test_questions_scope_is_checked(self):
        p = self.run_story("questions", self.path, "--parts", "blimp", expect=1)
        self.assertIn("'blimp' not a part id", p.stderr)
        p = self.run_story("questions", self.path, "--parts", "admin", expect=1)
        self.assertIn("no middle scene shows admin", p.stderr)
        p = self.run_story("scene", self.path, "1", "--parts", "db", expect=1)
        self.assertIn("--parts belongs to `story questions`", p.stderr)


class VisualizeTourState(Sandbox):
    """`tour-state`: saved progress in the project's own vault folder, claim-before-write."""

    REL = "Projects/quokka/visualize/tour-state.json"

    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        gt = self.vault / "Projects" / "golden-thread"
        (gt / "tools").mkdir(parents=True)
        (gt / "sessions").mkdir()
        shutil.copy(TOOLS / "gt_session.py", gt / "tools" / "gt_session.py")
        (self.vault / "Projects" / "quokka").mkdir()
        self.state = self.vault / self.REL
        self.story = self.tmp / "story.json"
        self.story.write_text(json.dumps(story()), encoding="utf-8")

    def tour(self, action, *args, expect=0):
        p = self.py(SCRIPT, "tour-state", action, "--project", "quokka", *args,
                    "--vault", self.vault)
        self.assertEqual(p.returncode, expect, p.stdout + p.stderr)
        return p

    def vault_files(self):
        return sorted(str(p.relative_to(self.vault)) for p in self.vault.rglob("*"))

    def test_set_then_get_round_trips_with_the_private_header(self):
        self.assertFalse(json.loads(self.tour("get").stdout)["saved"])
        self.tour("set", "--story", self.story, "--scene", "A request")
        self.tour("set", "--scene", "3", "--stepped-into", "gate", "--stepped-into", "gate")
        raw = self.state.read_text(encoding="utf-8")
        d = json.loads(raw)
        self.assertEqual(next(iter(d)), "_header", "the header comes first in the file")
        for words in ("PRIVATE", "NOT A METRIC"):
            self.assertIn(words, d["_header"])
        got = json.loads(self.tour("get").stdout)
        self.assertEqual((got["saved"], got["scene"], got["seen"], got["stepped_into"]),
                         (True, 3, [2, 3], ["gate"]))
        self.assertEqual(got["story_title"], "How the kestrel pipeline works")
        self.assertFalse(got["story_changed"])
        self.story.write_text(json.dumps(story(subtitle="A different story now")), "utf-8")
        self.assertTrue(json.loads(self.tour("get").stdout)["story_changed"])
        self.assertEqual(list(self.state.parent.iterdir()), [self.state], "no temp left behind")

    def test_dry_run_writes_nothing(self):
        before = self.vault_files()
        p = self.tour("set", "--story", self.story, "--scene", "2", "--dry-run")
        self.assertIn("dry run: would write %s" % self.REL, p.stdout)
        self.assertIn("NOT A METRIC", p.stdout)
        self.assertEqual(self.vault_files(), before)
        self.tour("set", "--story", self.story, "--scene", "2")
        saved = self.state.read_bytes()
        self.tour("set", "--scene", "4", "--finished", "--dry-run")
        self.assertEqual(self.state.read_bytes(), saved)

    def test_a_file_claimed_by_another_live_session_is_refused(self):
        self.tour("set", "--story", self.story, "--scene", "2")
        saved = self.state.read_bytes()
        p = self.py(self.vault / "Projects/golden-thread/tools/gt_session.py",
                    "--vault", self.vault, "--id", "wombat-live", "register", "--task", "t",
                    "--files", self.REL, env={"CLAUDE_PID": str(os.getpid())})
        self.assertOk(p)
        p = self.tour("set", "--scene", "3", expect=5)
        self.assertIn("claimed by live session wombat-live", p.stderr)
        self.assertEqual(self.state.read_bytes(), saved)
        # The holder itself may write.
        p = self.run_cmd([PYTHON, SCRIPT, "tour-state", "set", "--project", "quokka",
                          "--scene", "3", "--vault", self.vault],
                         env={"CLAUDE_SESSION_ID": "wombat-live",
                              "CLAUDE_PID": str(os.getpid())})
        self.assertOk(p)
        self.assertEqual(json.loads(self.state.read_text("utf-8"))["scene"], 3)

    def test_bad_input_is_refused_before_any_write(self):
        before = self.vault_files()
        for args, want in ((("set", "--scene", "2"), "no story recorded"),
                           (("set", "--story", self.story, "--scene", "9"), "no scene"),
                           (("set", "--story", self.story, "--stepped-into", "blimp"),
                            "no part 'blimp'"),
                           (("get", "--scene", "2"), "only reads")):
            with self.subTest(args=args):
                p = self.tour(*args, expect=1)
                self.assertIn(want, p.stderr)
        p = self.py(SCRIPT, "tour-state", "set", "--project", "ocelot", "--vault", self.vault)
        self.assertEqual(p.returncode, 1)
        self.assertIn("no project ocelot", p.stderr)
        p = self.py(SCRIPT, "tour-state", "get", "--project", "../x", "--vault", self.vault)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.vault_files(), before)


WALKTHROUGH = ("tour", "whatis", "trace", "explain-back")


class VisualizeWalkthroughSkills(unittest.TestCase):
    def text(self, name):
        return (VIS / "skills" / name / "SKILL.md").read_text(encoding="utf-8")

    def test_the_four_skills_exist_with_triggers_and_a_prediction_pause(self):
        for name in WALKTHROUGH:
            with self.subTest(skill=name):
                t = self.text(name)
                self.assertRegex(t, r"\A---\nname: %s\ndescription: " % re.escape(name))
                self.assertIn("Use when the user says:", t)
                self.assertIn("/gt-visualize:%s" % name, t)
                self.assertIn("Prediction pause", t)
                self.assertIn("gt_visualize.py", t)
        main = SKILL.read_text(encoding="utf-8")
        for name in WALKTHROUGH:
            self.assertIn("/gt-visualize:%s" % name, main)

    def test_trace_labels_static_and_only_offers_the_test(self):
        t = self.text("trace")
        self.assertIn("STATIC walkthrough", t)
        self.assertIn("no invented values", t.lower())
        self.assertIn("only on the user's yes", t)

    def test_explain_back_is_opt_in_and_wired_into_nothing_automatic(self):
        self.assertIn("Opt-in only", self.text("explain-back"))
        roots = [GT] + [latest_version_dir(REPO / n) for n in (
            "golden-thread-flow", "golden-thread-demo", "golden-thread-watch",
            "golden-thread-farm", "golden-thread-report-card", "golden-thread-usage")
            if (REPO / n).is_dir()]
        for root in roots:
            for p in root.rglob("*"):
                if p.is_file() and p.suffix in (".py", ".sh", ".md", ".json"):
                    self.assertNotIn("explain-back", p.read_text("utf-8", "replace"),
                                     "%s refers to explain-back" % p)
        self.assertIn("Never run it unasked", SKILL.read_text(encoding="utf-8"))

    def test_no_file_names_the_third_party_plugin(self):
        # Assembled from pieces so this file does not itself carry the names (the scrub gate
        # matches the organisation's first four letters as a whole word, hence the split).
        forbidden = ["review" + "-process", "IS" + "BN" + "Cloud" + "Ops"]
        for p in sorted(VIS.rglob("*")):
            if not p.is_file():
                continue
            text = p.read_text("utf-8", "replace").lower()
            for word in forbidden:
                self.assertNotIn(word.lower(), text, "%s names %s" % (p.relative_to(VIS),
                                                                     word[:4] + "..."))


class VisualizeInstall(Sandbox):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "src" / MARKET
        self.repo.mkdir(parents=True)
        shutil.copy2(INSTALL, self.repo / "install.sh")
        shutil.copytree(GT, self.repo / "golden-thread" / GT.name, ignore=IGNORE)
        shutil.copytree(WIKI, self.repo / "golden-thread-wiki" / WIKI.name, ignore=IGNORE)
        shutil.copytree(VIS, self.repo / "golden-thread-visualize" / VIS.name, ignore=IGNORE)
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
        key = "gt-visualize@%s" % MARKET
        cache = self.claude / "plugins" / "cache" / MARKET / "gt-visualize"
        self.install()
        self.assertTrue(list(cache.glob("*/skills/gt-visualize/SKILL.md")), "skill not installed")
        self.assertTrue(list(cache.glob("*/scripts/gt_visualize.py")), "script not installed")
        self.assertTrue(list(cache.glob("*/scripts/vendor/three-bundle.min.js")), "three.js not installed")
        self.assertIn(key, self.jload("plugins/installed_plugins.json")["plugins"])
        self.assertIs(self.jload("settings.json")["enabledPlugins"].get(key), True)

        self.install("--without", "visualize")
        self.assertFalse(cache.exists(), "module cache left behind")
        self.assertFalse(list((self.claude / "plugins").rglob("skills/gt-visualize")),
                         "a plugin still carries the skill")
        choices = (self.jload("golden-thread/install-choices.json", {}) or {}).get("choices", {})
        self.assertEqual(choices.get("visualize"), "off")


if __name__ == "__main__":
    unittest.main()


CLEAN_PAGE = "<!doctype html><html><head><title>Kestrel walkthrough</title></head>" \
             "<body><p>How the kestrel pipeline works.</p></body></html>\n"
FAKE_KEY = "Zx9pQ2mL" + "7vT4rB8n" + "K3wY6sD1" + "fH5jA0cE"
FAKE_GH = """#!/bin/sh
echo "$@" >> "%(log)s"
case "$1 $2" in
  "auth status") exit 0 ;;
  "gist create") echo "https://gist.github.com/shaven/abc123def" ;;
  "gist edit") exit 0 ;;
esac
"""


class VisualizePublish(Sandbox):
    """Feature request 2026-09-30-gt-visualize-default-publish-target."""

    def setUp(self):
        super().setUp()
        self.page = self.tmp / "kestrel-walkthrough.html"
        self.page.write_text(CLEAN_PAGE, encoding="utf-8")
        self.state = self.home / ".claude" / "golden-thread"

    def publish(self, *args, page=None):
        return self.py(SCRIPT, "publish", page or self.page, *args)

    def records(self):
        p = self.state / "visualize-publishes.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def settings(self, **kv):
        (self.home / ".claude" / "vault-config.json").write_text(json.dumps(kv))

    def fake_gh(self):
        d = self.tmp / "ghbin"
        d.mkdir()
        log = self.tmp / "gh.log"
        (d / "gh").write_text(FAKE_GH % {"log": log})
        (d / "gh").chmod(0o755)
        self.env["PATH"] = "%s:%s" % (d, self.env["PATH"])
        return log

    def test_the_module_registers_both_settings(self):
        m = json.loads((VIS / "module.json").read_text(encoding="utf-8"))
        keys = {s["key"]: s for s in m["settings"]}
        self.assertEqual(keys["visualize_publish"]["default"], "local")
        self.assertEqual(keys["visualize_publish"]["values"],
                         ["local", "claude", "github-pages", "gist"])
        self.assertEqual(keys["visualize_publish_visibility"]["values"],
                         ["private", "link", "public"])

    def test_local_shows_the_plan_and_waits_for_yes(self):
        p = self.publish("--check")
        self.assertOk(p)
        self.assertIn("plan: kestrel-walkthrough.html -> local", p.stdout)
        p = self.publish()
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertIn("re-run with --yes", p.stdout)
        self.assertEqual(self.records(), [], "something was recorded without --yes")
        self.assertOk(self.publish("--yes"))
        (rec,) = self.records()
        self.assertEqual((rec["target"], rec["visibility"]), ("local", "private"))
        self.assertTrue(rec["url"].startswith("file://"))
        self.assertIn("kestrel-walkthrough.html", self.py(SCRIPT, "publishes").stdout)

    def test_a_visibility_the_target_cannot_deliver_is_refused(self):
        for target, vis in (("github-pages", "private"), ("gist", "private"),
                            ("claude", "public")):
            with self.subTest(target=target):
                p = self.publish("--target", target, "--visibility", vis)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertIn("cannot be published as %s" % vis, p.stderr)

    def test_the_scrub_gate_refuses_what_should_not_leave(self):
        terms = self.tmp / "terms.txt"
        terms.write_text("# employer names\nquokkacorp\n")
        self.assertOk(self.py(SCRIPT, "targets", "set", "scrub_terms=%s" % terms))
        cases = {
            "IPv4": "<p>db at 10.20.30.40</p>",
            "home-folder": "<p>/Users/alice/projects</p>",
            "scrub term": "<p>built for QuokkaCorp</p>",
            # Assembled at run time: a credential-shaped LITERAL in the source trips the
            # repo's own secrets gate, which is right to refuse one.
            "credential": "<p>api_key = \"%s\"</p>" % FAKE_KEY,
        }
        for what, snippet in cases.items():
            with self.subTest(what=what):
                page = self.tmp / ("bad-%s.html" % what.replace(" ", "-"))
                page.write_text(CLEAN_PAGE.replace("</body>", snippet + "</body>"))
                p = self.publish("--check", page=page)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertIn("not published", p.stderr)
                self.assertIn(what if what != "credential" else "credential scan", p.stderr)
                self.assertNotIn(FAKE_KEY, p.stdout + p.stderr,
                                 "the credential's value was printed")
                self.assertNotIn("10.20.30.40", p.stdout + p.stderr)

    def test_the_settings_choose_the_default_target(self):
        log = self.fake_gh()
        self.settings(visualize_publish="gist", visualize_publish_visibility="link")
        p = self.publish("--check")
        self.assertOk(p)
        self.assertIn("-> gist, visibility link", p.stdout)
        self.assertIn("shows a gist's HTML as SOURCE", p.stdout)

    def test_github_pages_commits_pushes_and_records_the_url(self):
        bare = self.tmp / "site.git"
        clone = self.tmp / "site"
        self.run_cmd(["git", "init", "-q", "--bare", "-b", "main", str(bare)])
        self.run_cmd(["git", "clone", "-q", str(bare), str(clone)])
        self.run_cmd(["git", "-C", str(clone), "commit", "-q", "--allow-empty", "-m", "root"])
        self.run_cmd(["git", "-C", str(clone), "push", "-q", "origin", "HEAD:main"])
        self.assertOk(self.py(SCRIPT, "targets", "set", "github-pages", "repo=%s" % clone,
                              "base_url=https://example.github.io/site"))
        p = self.publish("--target", "github-pages", "--visibility", "public", "--yes")
        self.assertOk(p)
        url = "https://example.github.io/site/visualize/kestrel-walkthrough/"
        self.assertIn(url, p.stdout)
        shown = self.run_cmd(["git", "-C", str(bare), "show",
                              "main:visualize/kestrel-walkthrough/index.html"])
        self.assertEqual(shown.stdout, CLEAN_PAGE, "the page did not reach the remote")
        self.assertEqual(self.records()[-1]["url"], url)

    def test_github_pages_without_configuration_says_what_to_set(self):
        p = self.publish("--target", "github-pages", "--visibility", "public", "--check")
        self.assertEqual(p.returncode, 1)
        self.assertIn("targets set github-pages repo=", p.stderr)

    def test_gist_creates_once_then_edits_the_same_gist(self):
        log = self.fake_gh()
        p = self.publish("--target", "gist", "--visibility", "link", "--yes")
        self.assertOk(p)
        self.assertIn("https://gist.github.com/shaven/abc123def", p.stdout)
        calls = log.read_text()
        self.assertIn("gist create", calls)
        self.assertNotIn("--public", calls, "a link-visibility gist was made public")
        p = self.publish("--target", "gist", "--visibility", "link", "--yes")
        self.assertOk(p)
        self.assertIn("gist edit abc123def --filename kestrel-walkthrough.html", log.read_text())
        self.assertEqual(len(self.records()), 2)

    def test_claude_hands_over_then_records_the_artifact_url(self):
        p = self.publish("--target", "claude", "--yes")
        self.assertOk(p)
        self.assertIn("ready for Claude", p.stdout)
        self.assertEqual(self.records(), [], "recorded before the Artifact existed")
        url = "https://claude.ai/code/artifact/0000-kestrel"
        self.assertOk(self.publish("--target", "claude", "--url", url, "--yes"))
        self.assertEqual(self.records()[-1]["url"], url)
        p = self.publish("--target", "claude", "--yes")
        self.assertIn("as an update of %s" % url, p.stdout)

    def test_targets_never_store_a_credential(self):
        for pair in ("password=hunter2", "token=abc", "api_key=xyz"):
            with self.subTest(pair=pair):
                p = self.py(SCRIPT, "targets", "set", "github-pages", pair)
                self.assertEqual(p.returncode, 1)
                self.assertIn("credentials are never stored", p.stderr)
        p = self.py(SCRIPT, "targets", "set", "github-pages", "colour=red")
        self.assertEqual(p.returncode, 1)
        self.assertFalse((self.state / "visualize-targets.json").exists())

    def test_url_is_only_for_the_claude_target(self):
        p = self.publish("--target", "local", "--url", "https://example.com/x")
        self.assertEqual(p.returncode, 1)
        self.assertIn("--url records a claude.ai Artifact", p.stderr)
