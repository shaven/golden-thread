"""Modules that cannot run on native Windows are off there, said in words (0.20.0).

gt-lotr is a gateway served on a Unix-domain socket, its clients authenticated by peer uid and
its secrets protected by 0600 permission bits; Windows has none of the three. Before 0.20.0
`install.sh --with lotr` on Windows installed it, and the first command died on os.fchmod. Now
gt_components.module_detail (which install.sh and gt_doctor both read) turns it off on Windows,
whatever was chosen, and leaves the recorded choice alone. The lotr tests skip on Windows for
exactly this reason, which is a design limit of the module, not a defect being hidden.
"""
import unittest

from _harness import REPO, SCRIPTS, Sandbox, load_module


class PosixOnlyModules(Sandbox):
    def setUp(self):
        super().setUp()
        self.m = load_module(SCRIPTS / "gt_components.py", "gt_components_posix_only")

    def detail(self, windows, **kw):
        self.m._native_windows = lambda: windows
        return self.m.module_detail(str(REPO), home=str(self.home), **kw)

    def test_lotr_before_0_3_0_is_off_on_windows_even_when_chosen(self):
        self.assertTrue(self.m.posix_only("lotr", "0.2.0"))
        real = self.m.discover_modules

        def old_lotr(root):
            mods = real(root)
            for m in mods:
                if m["name"] == "lotr":
                    m["data"] = dict(m["data"], version="0.2.0")
            return mods
        self.m.discover_modules = old_lotr
        d = self.detail(True, with_=("lotr",))["lotr"]
        self.assertEqual(d["state"], "off")
        self.assertTrue(d["reason"].startswith("POSIX-only"), d["reason"])
        self.assertIn("Unix-domain-socket", d["reason"])

    def test_lotr_0_3_0_runs_on_windows_over_the_named_pipe(self):
        """0.20.0: gt-lotr 0.3.0 has a Windows front door (gt_ipc named pipe)."""
        self.assertFalse(self.m.posix_only("lotr", "0.3.0"))
        self.assertEqual(self.detail(True, with_=("lotr",))["lotr"]["state"], "on")

    def test_off_windows_the_choice_stands(self):
        self.assertEqual(self.detail(False, with_=("lotr",))["lotr"]["state"], "on")

    def test_no_other_module_is_affected(self):
        on, off = self.detail(False), self.detail(True)
        for name in on:
            if name not in self.m.POSIX_ONLY_MODULES:
                self.assertEqual(on[name]["state"], off[name]["state"], name)

    def test_the_installer_names_the_skip(self):
        text = (REPO / "install.sh").read_text(encoding="utf-8")
        self.assertIn('[ "$_why" = platform ]', text)
        self.assertIn("POSIX-only", text)


if __name__ == "__main__":
    unittest.main()
