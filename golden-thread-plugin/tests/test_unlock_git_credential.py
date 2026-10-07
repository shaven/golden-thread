"""gt_unlock.py git-credential: the order of its two questions (0.20.4).

The 0.20.3 beta asked for the gt:publish fingerprint FIRST and only then asked for the sealed
token, so a push started from an assistant session cost the owner two Touch IDs and was then
refused anyway (live test, 2026-10-06: `gt:secrets` deny `mcp_only` after two step_ups). The door
is decided before any prompt, so the helper asks it first, without a request, and refuses before
anyone is asked to touch. The authority is stubbed here: this is about the CLI's order, not its
verdicts (those have their own tests).
"""
import importlib.util
import io
import sys
import unittest
from unittest import mock

from _harness import SCRIPTS

sys.path.insert(0, str(SCRIPTS))


def _load():
    spec = importlib.util.spec_from_file_location("gt_unlock_cli_for_tests",
                                                  str(SCRIPTS / "gt_unlock.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GET = "protocol=https\nhost=github.com\n\n"
MAP = [{"protocol": "https", "host": "github.com", "username": "x-access-token",
        "ref": "sealed:github"}]


def verdict(allowed, code):
    return {"allowed": allowed, "code": code, "message": code, "grant": None,
            "level": "?", "hints": []}


class GitCredentialOrder(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.calls = []

    def run_get(self, secrets_code):
        def check(scope, request=False, reason="", **kw):
            self.calls.append((scope, bool(request)))
            if scope == "gt:secrets":
                return verdict(secrets_code in ("open", "granted"), secrets_code)
            return verdict(True, "granted")
        ns = mock.Mock(action="get")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.mod.C, "check", side_effect=check), \
                mock.patch.object(self.mod, "_credentials_map", return_value=MAP), \
                mock.patch.object(self.mod, "_resolve", return_value="not-a-real-token"), \
                mock.patch.object(sys, "stdin", io.StringIO(GET)), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            rc = self.mod.cmd_git_credential(ns)
        return rc, out.getvalue(), err.getvalue()

    def test_a_door_refusal_asks_for_no_fingerprint(self):
        for code in ("mcp_only", "denied", "failed_closed"):
            self.calls = []
            rc, out, err = self.run_get(code)
            self.assertEqual(rc, 1, code)
            self.assertEqual(out, "", code)
            self.assertNotIn(("gt:publish", True), self.calls,
                             "%s: the owner was asked to touch for a push that cannot happen" % code)
            self.assertIn("gt:secrets", err + " ".join(s for s, _ in self.calls), code)

    def test_an_allowed_caller_still_needs_the_fingerprint(self):
        rc, out, _err = self.run_get("open")
        self.assertEqual(rc, 0)
        self.assertIn(("gt:publish", True), self.calls)
        self.assertIn("password=not-a-real-token", out)

    def test_a_secret_that_would_prompt_is_not_a_refusal(self):
        """step_up / locked on gt:secrets just means the resolve will ask: carry on."""
        for code in ("step_up", "locked"):
            self.calls = []
            rc, _out, _err = self.run_get(code)
            self.assertEqual(rc, 0, code)
            self.assertIn(("gt:publish", True), self.calls, code)


if __name__ == "__main__":
    unittest.main()
