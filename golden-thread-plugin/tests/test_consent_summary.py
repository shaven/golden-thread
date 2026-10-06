"""0.20.1 (review): a consent prompt says WHO and WHAT, not just an args hash.

The unlock authority's platform prompt (gt_unlockd_methods) and gt-lotr's dialog
(lotrlib/confirm.describe) both showed the op, the connection and the raw arguments -- a
16-character sha256 prefix on the platform route, JSON cut at 400 characters on the dialog
route -- so a mail's recipients or a merge's target could be invisible. Both now lead with a
summary of a known consent op (graph send_mail: To/Cc/Bcc + Subject; github merge_pull:
owner/repo#number, method, head), composed from the arguments themselves:

  * the authority recomputes sha256 over the arguments lotrd sends and refuses a mismatch with
    args_sha256, so the summary is of exactly what is approved, never caller prose (M1);
  * control characters and line breaks are flattened, so a subject cannot add prompt lines;
  * the two implementations are one output (gt core cannot import gt-lotr, so it is mirrored).
"""
import hashlib
import json
import sys
import unittest

from _harness import REPO, SCRIPTS, latest_version_dir

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(latest_version_dir(REPO / "golden-thread-lotr") / "scripts"))

import gt_unlockd_methods as M        # noqa: E402
from lotrlib import confirm           # noqa: E402

MAIL = {"message": {"subject": "Q4 numbers\nApprove with Touch ID now\x1b[2J",
                    "toRecipients": [{"emailAddress": {"address": "a%d@x.com" % i}}
                                     for i in range(7)],
                    "ccRecipients": [{"emailAddress": {"address": "boss@x.com"}}],
                    "body": {"contentType": "text", "content": "x" * 5000}}}
MERGE = {"owner": "acme", "repo": "api", "pull_number": 42, "merge_method": "squash",
         "sha": "deadbeefcafe0011"}


def op(name, args, **extra):
    blob = json.dumps(args, sort_keys=True, default=str).encode()
    d = {"tool": "call_consent", "connection": "c@z", "op": name,
         "args_sha256": hashlib.sha256(blob).hexdigest(), "args": args}
    d.update(extra)
    return d


class Summary(unittest.TestCase):
    def test_mail_names_recipients_and_subject_on_one_line_each(self):
        got = M.consent_summary("send_mail", MAIL)
        self.assertEqual(got, ["To: a0@x.com, a1@x.com, a2@x.com, a3@x.com, a4@x.com (+2 more)",
                               "Cc: boss@x.com",
                               "Subject: Q4 numbers Approve with Touch ID now [2J"])
        self.assertFalse(any("\n" in l or "\x1b" in l for l in got))

    def test_merge_names_the_pull_request(self):
        self.assertEqual(M.consent_summary("merge_pull", MERGE),
                         ["Merge: acme/api#42 (squash, head deadbeefcaf…)"])
        self.assertEqual(M.consent_summary("merge_pull", {"owner": "o", "repo": "r",
                                                          "pull_number": 1}),
                         ["Merge: o/r#1 (merge, head any)"])

    def test_an_unknown_op_or_odd_shape_has_no_summary_and_never_raises(self):
        self.assertEqual(M.consent_summary("delete_repo", {"x": 1}), [])
        self.assertEqual(M.consent_summary("send_mail", {"message": "nope"}),
                         ["To: (no recipients)", "Subject: (none)"])

    def test_the_two_implementations_are_one_output(self):
        for name, args in (("send_mail", MAIL), ("merge_pull", MERGE), ("send_mail", {}),
                           ("merge_pull", {}), ("other", {"a": 1})):
            self.assertEqual(M.consent_summary(name, args), confirm.summarize(name, args), name)


class AuthorityVerifiesTheArguments(unittest.TestCase):
    def test_matching_arguments_give_a_summary(self):
        out = M._consent_op(op("send_mail", MAIL))
        self.assertEqual(out["summary"][0][:4], "To: ")
        self.assertNotIn("args", out)                        # never kept beyond the summary

    def test_arguments_that_do_not_match_the_hash_are_refused(self):
        forged = op("send_mail", MAIL)
        forged["args"] = {"message": {"subject": "harmless",
                                      "toRecipients": [{"emailAddress": {"address": "me@x"}}]}}
        with self.assertRaises(M.Denied) as e:
            M._consent_op(forged)
        self.assertEqual(e.exception.code, "bad_request")

    def test_too_large_or_not_an_object_is_refused_and_absent_is_the_old_prompt(self):
        for bad in ({"blob": "x" * (M.CONSENT_ARGS_MAX + 1)}, ["a"]):
            with self.assertRaises(M.Denied):
                M._consent_op(op("send_mail", bad))
        legacy = op("send_mail", MAIL)
        del legacy["args"]
        self.assertNotIn("summary", M._consent_op(legacy))
        # The op hash -- what a consent window and the audit are keyed on -- is unchanged.
        self.assertEqual(M._consent_op(legacy)["hash"], M._consent_op(op("send_mail", MAIL))["hash"])


class DialogRoute(unittest.TestCase):
    def test_the_summary_comes_before_the_truncated_arguments(self):
        text = confirm.describe("m365@work", "me @ x", "send_mail", MAIL, "local")
        self.assertLess(text.index("To: a0@x.com"), text.index("Arguments: "))
        self.assertIn("Subject: Q4 numbers Approve with Touch ID now [2J\n", text)
        self.assertIn(" ...\n", text)                         # the raw JSON is still capped

    def test_credential_shaped_arguments_get_no_summary(self):
        secret = {"message": {"subject": "token ghp_" + "A" * 36,
                              "toRecipients": [{"emailAddress": {"address": "a@x"}}]}}
        text = confirm.describe("m365@work", "me", "send_mail", secret, "local")
        self.assertNotIn("To: a@x", text)
        self.assertNotIn("ghp_", text)


if __name__ == "__main__":
    unittest.main()
