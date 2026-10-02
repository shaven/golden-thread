"""No `printf/echo "$X" | grep -q` in a pipefail script (0.18.1).

Under `set -o pipefail`, grep -q exits on its first match; the writer can then die of SIGPIPE,
the pipeline's status becomes non-zero, and a line that IS present reads as absent. It made
install.sh report "unrecognised output" for vault upgrades and package.sh report "no installable
gt version directory found" -- intermittently, so both were filed as load flakes for a while.
Here-strings (`grep -q P <<<"$X"`) have no pipe and no race.
"""
import re
import sys
import unittest

from _harness import REPO, needs_dev

PIPE_INTO_GREP_Q = re.compile(r'^(?!\s*#).*\b(printf|echo)\b[^|#]*\|\s*grep\s+-q', re.M)


@needs_dev
class NoSigpipeGrepQ(unittest.TestCase):
    def shipped_scripts(self):
        """The root scripts, dev/, and every plugin's NEWEST release (older ones are history)."""
        sys.path.insert(0, str(REPO / "dev"))
        import plugins                                            # noqa: PLC0415
        files = [REPO / n for n in ("install.sh", "package.sh", "selftest.sh")]
        files += sorted((REPO / "dev").glob("*.sh"))
        for _, vdir, _ in plugins.discover(REPO):
            files += sorted(vdir.rglob("*.sh"))
        return [f for f in files if f.is_file()]

    def test_pipefail_scripts_never_pipe_a_variable_into_grep_q(self):
        bad = []
        for p in self.shipped_scripts():
            text = p.read_text(encoding="utf-8", errors="replace")
            if "pipefail" not in text:
                continue
            for m in PIPE_INTO_GREP_Q.finditer(text):
                bad.append("%s:%d: %s" % (p.name, text.count("\n", 0, m.start()) + 1,
                                          m.group(0).strip()[:100]))
        self.assertEqual(bad, [], "use `grep -q PATTERN <<<\"$VAR\"` instead:\n" + "\n".join(bad))

    def test_the_rule_catches_the_shape_it_is_for(self):
        self.assertTrue(PIPE_INTO_GREP_Q.search("""  if printf '%s\\n' "$out" | grep -q 'x'; then"""))
        self.assertFalse(PIPE_INTO_GREP_Q.search("""  if grep -q 'x' <<<"$out"; then"""))
        self.assertFalse(PIPE_INTO_GREP_Q.search("""  # never `printf "$out" | grep -q`"""))


if __name__ == "__main__":
    unittest.main()
