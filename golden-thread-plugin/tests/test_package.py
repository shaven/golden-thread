"""package.sh: builds golden-thread-plugin.zip, the artifact INSTALL.md hands out.

package.sh cds to its own directory and writes the zip there, so every run uses a
throwaway copy of the repo. The copy carries decoy version directories to prove
the version is picked numerically (0.9.13 over 0.9.9) and only from directories
with a plugin manifest.

Contracts pinned here:
  * the zip holds the newest gt and gt-wiki version dirs, hooks/ included, every
    shipped file intact, and nothing from older or manifest-less dirs;
  * MANIFEST.json, __pycache__ and .DS_Store are not shipped; install.sh is +x;
  * a stale zip is replaced, not appended to;
  * the unzipped artifact installs cleanly into a sandbox HOME.
"""
import json
import shutil
import stat
import unittest
import zipfile
from pathlib import Path

from _harness import Sandbox, REPO, GT, WIKI, ENFORCEMENT_HOOKS

TOP = ("package.sh", "install.sh", "selftest.sh", "README.md", "INSTALL.md",
       "ONBOARDING.md", "MANUAL.md")
GT_DIRS = (".claude-plugin", "skills", "scripts", "templates", "hooks")
WIKI_DIRS = (".claude-plugin", "skills", "scripts", "templates", "commands", "hooks")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
DIST = "golden-thread-plugin"


def shipped_files(vdir: Path, dirs):
    return {str(p.relative_to(vdir)) for d in dirs if (vdir / d).is_dir()
            for p in (vdir / d).rglob("*") if p.is_file()}


class PackageTest(Sandbox):
    def setUp(self):
        super().setUp()
        if not (shutil.which("zip") and shutil.which("unzip")):
            self.skipTest("zip/unzip not installed")
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        for f in TOP:
            if (REPO / f).is_file():
                shutil.copy2(REPO / f, self.repo / f)
        gt, wiki = self.repo / "golden-thread", self.repo / "golden-thread-wiki"
        shutil.copytree(GT, gt / GT.name, ignore=IGNORE)
        shutil.copytree(WIKI, wiki / WIKI.name, ignore=IGNORE)
        # decoys: lexically newer but numerically older, and a manifest-less "newest"
        for root, name in ((gt, "0.9.9"), (wiki, "0.1.0")):
            d = root / name / ".claude-plugin"
            d.mkdir(parents=True)
            (d / "plugin.json").write_text(json.dumps({"name": "decoy", "version": name}))
            (root / name / "scripts").mkdir()
            (root / name / "scripts" / "decoy.py").write_text("")
        (gt / "99.0.0" / "scripts").mkdir(parents=True)
        (gt / "99.0.0" / "scripts" / "wip.py").write_text("")
        # junk that must be stripped from the artifact
        (gt / GT.name / "MANIFEST.json").write_text("{}")
        (gt / GT.name / "scripts" / "__pycache__").mkdir(exist_ok=True)
        (gt / GT.name / "scripts" / "__pycache__" / "x.cpython-39.pyc").write_bytes(b"\0")
        (gt / GT.name / "skills" / ".DS_Store").write_bytes(b"\0")

    def package(self):
        p = self.sh(self.repo / "package.sh", timeout=300)
        self.assertOk(p)
        z = self.repo / "golden-thread-plugin.zip"
        self.assertTrue(z.is_file(), "no zip produced")
        return p, z

    def test_zip_ships_the_newest_versions_with_hooks(self):
        p, z = self.package()
        self.assertIn(f"gt {GT.name}, gt-wiki {WIKI.name}", p.stdout)
        with zipfile.ZipFile(z) as zf:
            names = {n for n in zf.namelist() if not n.endswith("/")}
        gt_prefix = f"{DIST}/golden-thread/{GT.name}/"
        wiki_prefix = f"{DIST}/golden-thread-wiki/{WIKI.name}/"
        src_gt = {f for f in shipped_files(GT, GT_DIRS)
                  if "__pycache__" not in f and not f.endswith(".DS_Store")}
        got_gt = {n[len(gt_prefix):] for n in names if n.startswith(gt_prefix)}
        self.assertEqual(got_gt, src_gt, "gt files missing from or extra in the zip")
        self.assertTrue(any(n.startswith(gt_prefix + "hooks/") for n in names), "hooks/ not shipped")
        for hook in ENFORCEMENT_HOOKS:
            self.assertIn(gt_prefix + "hooks/" + hook, names)
        got_wiki = {n[len(wiki_prefix):] for n in names if n.startswith(wiki_prefix)}
        self.assertEqual(got_wiki, {f for f in shipped_files(WIKI, WIKI_DIRS) if "__pycache__" not in f})
        versions = {n.split("/")[2] for n in names if n.startswith(f"{DIST}/golden-thread")
                    and n.count("/") > 2}
        self.assertEqual(versions, {GT.name, WIKI.name}, "an older or manifest-less version was shipped")
        for junk in ("MANIFEST.json", "__pycache__", ".DS_Store", ".pyc"):
            self.assertFalse([n for n in names if junk in n], f"{junk} shipped")
        for f in ("install.sh", "selftest.sh", "INSTALL.md"):
            self.assertIn(f"{DIST}/{f}", names)
        self.assertNotIn(f"{DIST}/package.sh", names)

    def test_install_sh_is_executable_in_the_zip(self):
        _, z = self.package()
        with zipfile.ZipFile(z) as zf:
            mode = zf.getinfo(f"{DIST}/install.sh").external_attr >> 16
        self.assertTrue(mode & stat.S_IXUSR, oct(mode))

    def test_a_stale_zip_is_replaced(self):
        stale = self.repo / "golden-thread-plugin.zip"
        with zipfile.ZipFile(stale, "w") as zf:
            zf.writestr(f"{DIST}/golden-thread/0.1.0/stale.txt", "old")
        _, z = self.package()
        with zipfile.ZipFile(z) as zf:
            self.assertFalse([n for n in zf.namelist() if "stale.txt" in n])

    def test_the_artifact_installs(self):
        _, z = self.package()
        out = self.tmp / "unpacked"
        self.assertOk(self.run_cmd(["unzip", "-q", z, "-d", out]))
        p = self.sh(out / DIST / "install.sh", timeout=300)
        self.assertOk(p, "install.sh from the zip failed")
        self.assertIn(f"Installing gt {GT.name}", p.stdout)
        self.assertIn("Verified hook wiring", p.stdout)
        self.assertTrue((self.home / ".claude" / "golden-thread" / "hooks" / "inject_core_rules.sh").is_file())


if __name__ == "__main__":
    unittest.main()
