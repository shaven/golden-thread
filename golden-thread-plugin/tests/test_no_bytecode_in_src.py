"""Nothing gt runs writes into gt-src (0.19.0, request 2026-10-02-hooks-write-bytecode-into-gt-src).

Found 2026-10-02 on the publishing Mac: SessionStart hooks are handed the gt-src release path
and imported from it with plain `python3`, so `__pycache__/*.pyc` landed in the publish folder
and gt-src stopped matching its SHA256SUMS. These tests publish a fixture gt-src, run every
hook command install.sh would register and a doctor run against it, and assert the tree is
byte-for-byte what it was.
"""
import hashlib
import os
import shlex
import shutil
import subprocess
import unittest
from pathlib import Path

from _harness import GT, Sandbox, load_module


def snapshot(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for n in filenames + [d + "/" for d in dirnames]:
            p = os.path.join(dirpath, n.rstrip("/"))
            rel = os.path.relpath(p, root) + ("/" if n.endswith("/") else "")
            out[rel] = "" if n.endswith("/") else hashlib.sha256(Path(p).read_bytes()).hexdigest()
    return out


class NoBytecodeInSrc(Sandbox):

    def setUp(self):
        super().setUp()
        # The bytecode switch must come from gt itself, never from whoever runs the suite.
        self.env.pop("PYTHONDONTWRITEBYTECODE", None)
        self.plugin_root = self.tmp / "gt-src" / "golden-thread-plugin"
        self.release = self.plugin_root / "golden-thread" / GT.name
        shutil.copytree(GT, self.release, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.hooks = self.home / ".claude" / "golden-thread" / "hooks"
        self.hooks.mkdir(parents=True)
        for sub in ("hooks", "scripts"):
            for f in (self.release / sub).iterdir():
                if f.is_file():
                    shutil.copy2(f, self.hooks / f.name)
        self.vault = self.tmp / "vault"
        (self.vault / "Projects" / "golden-thread").mkdir(parents=True)
        self.before = snapshot(self.release)

    def hook_commands(self):
        saved = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        try:
            comp = load_module(GT / "scripts" / "gt_components.py", "gt_components_nobc")
            return comp.hook_commands(str(self.release), plugin_root=str(self.plugin_root),
                                      home=str(self.home))
        finally:
            os.environ["HOME"] = saved

    def run_shell(self, command):
        return subprocess.run(["bash", "-c", command], env=self.env, cwd=str(self.tmp),
                              capture_output=True, text=True, timeout=120)

    def assertSrcUnchanged(self):
        after = snapshot(self.release)
        added = sorted(set(after) - set(self.before))
        changed = sorted(k for k in self.before if k in after and after[k] != self.before[k])
        self.assertEqual((added, changed), ([], []), "gt-src was written to")

    def test_every_python_hook_command_runs_with_bytecode_off(self):
        rows = [r for r in self.hook_commands() if r["script"].endswith(".py")]
        self.assertTrue(rows)
        for r in rows:
            with self.subTest(script=r["script"], event=r["event"]):
                self.assertEqual(shlex.split(r["command"])[:2], ["python3", "-B"], r["command"])

    def test_hook_commands_leave_gt_src_and_the_hooks_dir_untouched(self):
        for r in self.hook_commands():
            self.run_shell(r["command"])
        self.assertSrcUnchanged()
        self.assertEqual(list(self.hooks.rglob("__pycache__")), [])

    def test_a_doctor_run_from_gt_src_writes_nothing_there(self):
        for args in ([], ["post-install"]):
            with self.subTest(args=args):
                subprocess.run(["python3", str(self.release / "scripts" / "gt_doctor.py")] + args
                               + ["--vault", str(self.vault)], env=self.env, cwd=str(self.tmp),
                               capture_output=True, text=True, timeout=300)
        self.assertSrcUnchanged()


if __name__ == "__main__":
    unittest.main()
