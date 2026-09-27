"""gt_secrets: the scanner that must never become the thing that leaks.

The FIRST test here is the leak test, deliberately. The 0.16.0 attempt was cut because it
leaked the values it redacted -- its `secrets` check printed a length while its `naming`
check, same tool same run, printed raw source text. Every other test in this file is about
detection; that one is about the contract, and if it ever fails the tool must not ship
however well it detects.

The planted tokens below are OBVIOUS FAKES built from public vendor prefixes. Nothing here
is a real credential, and no test asserts on a real one.
"""
import json
import unittest

from _harness import Sandbox, SCRIPTS

SEC = SCRIPTS / "gt_secrets.py"

# Deliberately fake, deliberately distinctive: a string we can grep every output for.
FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"[:16]
FAKE_ASSIGNED = "Zk9qP2wX7bV4nM8tR1sL6yH3"


class SecretsBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.tree = self.tmp / "tree"
        self.tree.mkdir()

    def write(self, rel, text):
        p = self.tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def scan(self, *extra, expect=None):
        proc = self.py(SEC, self.tree, *extra)
        if expect is not None:
            self.assertEqual(proc.returncode, expect,
                             "exit %d\n%s\n%s" % (proc.returncode, proc.stdout, proc.stderr))
        return proc


class TheLeakTest(SecretsBase):
    """The one that decides whether this tool may ship at all."""

    def test_no_output_mode_ever_contains_the_matched_value(self):
        self.write("app/config.py", 'AWS_KEY = "%s"\n' % FAKE_AWS)
        self.write(".env", "DB_PASSWORD=%s\n" % FAKE_ASSIGNED)
        base = self.tmp / "base.json"

        streams = []
        for args in ([], ["--json"], ["--write-baseline", str(base)]):
            proc = self.py(SEC, self.tree, *args)
            streams.append(("stdout %s" % args, proc.stdout))
            streams.append(("stderr %s" % args, proc.stderr))
        if base.is_file():
            streams.append(("baseline file", base.read_text()))

        for label, blob in streams:
            for planted in (FAKE_AWS, FAKE_ASSIGNED):
                self.assertNotIn(planted, blob,
                                 "%s CONTAINS THE MATCHED VALUE — the tool leaked what it "
                                 "was protecting. This is the failure that cut 0.16.0." % label)

    def test_it_still_found_them(self):
        """A tool that finds nothing trivially passes the leak test."""
        self.write("app/config.py", 'AWS_KEY = "%s"\n' % FAKE_AWS)
        proc = self.scan(expect=1)
        self.assertIn("aws.access-key-id", proc.stdout)
        self.assertIn("app/config.py", proc.stdout)

    def test_a_finding_carries_length_not_the_value(self):
        self.write("k.txt", FAKE_AWS + "\n")
        proc = self.scan("--json", expect=1)
        f = json.loads(proc.stdout)["findings"][0]
        self.assertEqual(set(f) - {"entropy"}, {"path", "line", "rule", "length"})
        self.assertEqual(f["length"], len(FAKE_AWS))


class DetectionRegressions(SecretsBase):
    """One test per numbered finding of the 2026-09-16 validation that cut 0.16.0."""

    def test_1_a_dotenv_full_of_credentials_is_not_zero_findings(self):
        self.write(".env", "DB_PASSWORD=%s\nAPI_TOKEN=%s\n" % (FAKE_ASSIGNED, FAKE_ASSIGNED))
        proc = self.scan("--json", expect=1)
        self.assertGreaterEqual(len(json.loads(proc.stdout)["findings"]), 2,
                                "the generic rule missed a .env — finding #1 all over again")

    def test_4_every_match_on_a_line_is_reported(self):
        self.write("min.js", " ".join([FAKE_AWS] * 5) + "\n")
        proc = self.scan("--json", expect=1)
        aws = [f for f in json.loads(proc.stdout)["findings"] if f["rule"] == "aws.access-key-id"]
        self.assertEqual(len(aws), 5, "finditer regression: five tokens reported as %d" % len(aws))

    def test_5_a_name_that_means_secrets_overrides_the_skip(self):
        big = self.write(".env", "x" * (3 * 1024 * 1024) + "\nTOKEN=%s\n" % FAKE_ASSIGNED)
        self.assertGreater(big.stat().st_size, 2 * 1024 * 1024)
        proc = self.scan("--json", expect=1)
        self.assertTrue(json.loads(proc.stdout)["findings"],
                        "an oversized .env was skipped — skips are where secrets live")

    def test_6_a_baseline_makes_the_second_run_quiet(self):
        self.write("app.py", 'KEY = "%s"\n' % FAKE_AWS)
        base = self.tmp / "b.json"
        self.py(SEC, self.tree, "--write-baseline", str(base))
        proc = self.scan("--baseline", str(base), expect=0)
        self.assertIn("0 new finding(s)", proc.stdout)


class TheAffirmative(SecretsBase):
    def test_a_clean_run_says_what_it_covered(self):
        self.write("ok.py", "x = 1\n")
        proc = self.scan(expect=0)
        self.assertIn("gt-secrets: clean", proc.stdout)
        self.assertRegex(proc.stdout, r"\d+ file\(s\) scanned")
        self.assertRegex(proc.stdout, r"\d+ rule\(s\)")

    def test_skips_are_counted_not_silent(self):
        self.write("ok.py", "x = 1\n")
        self.write("logo.png", "notreallyapng\n")
        proc = self.scan(expect=0)
        self.assertIn("skipped", proc.stdout)


class Isolation(unittest.TestCase):
    def test_it_is_not_a_member_of_any_aggregator(self):
        """Two tools with opposite output rules in one process is how 0.16.0 leaked."""
        for name in ("gt_scan.py", "gt_allin.py"):
            src = (SCRIPTS / name).read_text()
            self.assertNotIn("gt_secrets", src,
                             "%s references gt_secrets — it must run alone" % name)

    def test_every_writer_lives_inside_the_single_emitter(self):
        """Not a count -- a location. Every print must sit in class Out, which is the one
        place that could ever emit a value and takes none. Counting print sites was the
        first version of this test and it fails for the wrong reason: adding an honest
        status line broke it while a leak inside Out would not have."""
        src = (SCRIPTS / "gt_secrets.py").read_text()
        cls = src.index("class Out")
        end = src.index("\ndef ", cls)
        outside = [l.strip() for l in (src[:cls] + src[end:]).splitlines()
                   if l.strip().startswith("print(")]
        self.assertEqual(outside, [],
                         "print outside class Out — every writer must go through the one "
                         "emitter:\n  " + "\n  ".join(outside))


if __name__ == "__main__":
    unittest.main()


class FalsePositivesMeasuredOnThisRepo(SecretsBase):
    """Each of these was a REAL finding on the plugin repo on 2026-09-27, and each was
    wrong. They are tests rather than baseline entries because a baselined finding is
    invisible, while a fixed rule never fires and the reason sits next to the code.

    First scan: 48 findings. After scoping out archived releases: 10. After these three
    rules: 0. Nothing was suppressed to get there.
    """

    def test_a_function_call_is_not_a_credential(self):
        """Seven of ten: the rule was capturing the CALL, not a value."""
        self.write("a.py", "token = build_token(user, ttl=30)\n")
        self.scan(expect=0)

    def test_a_shell_variable_reference_is_not_a_credential(self):
        self.write("b.sh", 'PASSWORD=$DB_PASSWORD\nTOKEN="${CI_TOKEN}"\n')
        self.scan(expect=0)

    def test_a_sudoers_nopasswd_line_is_not_a_credential(self):
        """NOPASSWD contains PASSWD, and its 'value' is a command path. This shape is in
        real sudoers files everywhere, so it would have fired constantly."""
        self.write("sudoers", "someone ALL=(ALL) NOPASSWD: /usr/sbin/service nginx restart\n")
        self.scan(expect=0)

    def test_an_absolute_path_is_not_a_credential(self):
        self.write("c.conf", "credential_file = /etc/app/keys/id_rsa\n")
        self.scan(expect=0)

    def test_but_a_base64_looking_value_is_still_found(self):
        """The boundary of the path rule: `/` alone must not disqualify a value, because
        base64 uses it. A value that merely CONTAINS `/` is still a candidate.

        The fixture is ASSEMBLED rather than written as one literal, for the same reason
        FAKE_AWS is at the top of this file: a credential-shaped literal here is a finding
        when gt-secrets scans its own repo, and on 2026-09-27 this line was the one finding
        that stopped the suite writing a test receipt. Splitting it is the honest fix --
        excluding `tests/` would have been a suppression, and tests are code.
        """
        value = "aGVsbG8" + "/" + "d29ybGQrMTIzNDU2Nzg5MA" + "=="
        self.write("d.yml", "api_key: %s\n" % value)
        self.scan(expect=1)

    def test_and_the_vendor_rules_are_untouched_by_all_of_it(self):
        self.write("e.txt", "%s\n" % FAKE_AWS)
        proc = self.scan("--json", expect=1)
        self.assertEqual(json.loads(proc.stdout)["findings"][0]["rule"], "aws.access-key-id")


class ScopeIsReportedNotSilent(SecretsBase):
    def test_an_exclude_is_counted_in_the_affirmative(self):
        self.write("keep/a.py", "x = 1\n")
        self.write("drop/b.py", "y = 2\n")
        proc = self.scan("--exclude", "drop", expect=0)
        self.assertIn("excluded by 1 glob(s)", proc.stdout)

    def test_a_name_that_means_credentials_beats_an_exclude(self):
        """An --exclude must not become a way to hide a .env."""
        self.write("drop/.env", "SECRET=%s\n" % FAKE_ASSIGNED)
        self.scan("--exclude", "drop", expect=1)

    def test_binary_is_not_reported_as_unreadable(self):
        """A single .DS_Store used to make every run exit 2 — a permanent false failure,
        and the first thing anyone would have disabled."""
        (self.tree / ".DS_Store").write_bytes(b"\x00\x01\x02\xff\xfe")
        proc = self.scan(expect=0)
        self.assertIn("binary", proc.stdout)
        self.assertNotIn("UNREADABLE", proc.stdout)
