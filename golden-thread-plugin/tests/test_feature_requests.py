"""dev/feature_requests.py — the validator that runs before any code is written."""
import json
import unittest
from pathlib import Path

from _harness import Sandbox, REPO, PYTHON

FR = REPO / "dev" / "feature_requests.py"

GOOD = """---
id: 2026-01-02-example-thing
title: "Example thing"
requested_by: owner
date: 2026-01-02
category: feature
priority: low
breaking_change: no
owner_override: no
affected_components:
  - install.sh
  - skills/gt-lint
  - (new) scripts/gt_example.py
  - templates/example.md (new template)
---

# Example thing

## Motivation

Something is missing.

## Description

It should exist.

## Acceptance Criteria

- [ ] the example script prints hello when run
- [ ] the lint skill mentions the example

## Test Plan

A test runs the example script in a sandbox and checks stdout says hello; another greps the skill.
"""


class FeatureRequestTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.queue = self.tmp / "queue"
        for st in ("new", "reviewed", "accepted", "rejected", "implemented"):
            (self.queue / st).mkdir(parents=True)

    def write(self, text, name="2026-01-02-example-thing.md", stage="new"):
        p = self.queue / stage / name
        p.write_text(text, encoding="utf-8")
        return p

    def validate(self, path, *extra):
        return self.py(FR, "validate", path, "--src", REPO, "--json", *extra)

    def verdict(self, text, **kw):
        proc = self.validate(self.write(text, **kw))
        return proc.returncode, json.loads(proc.stdout)[0]

    def test_complete_request_is_ready(self):
        rc, r = self.verdict(GOOD)
        self.assertEqual((rc, r["verdict"]), (0, "ready"), r)

    def test_missing_test_plan_is_incomplete(self):
        rc, r = self.verdict(GOOD.split("## Test Plan")[0])
        self.assertEqual((rc, r["reason"]), (1, "incomplete"))

    def test_missing_key_is_incomplete(self):
        rc, r = self.verdict(GOOD.replace("priority: low\n", ""))
        self.assertEqual(r["reason"], "incomplete")
        self.assertIn("missing frontmatter key: priority", r["errors"])

    def test_bad_enum_value(self):
        rc, r = self.verdict(GOOD.replace("category: feature", "category: enhancement"))
        self.assertEqual((rc, r["reason"]), (1, "invalid-value"))

    def test_filename_must_equal_id(self):
        rc, r = self.verdict(GOOD, name="2026-01-02-other.md")
        self.assertEqual(r["reason"], "invalid-value")

    def test_date_must_match_id(self):
        rc, r = self.verdict(GOOD.replace("date: 2026-01-02", "date: 2026-01-03"))
        self.assertEqual(r["reason"], "invalid-value")

    def test_nonexistent_component_is_stale(self):
        rc, r = self.verdict(GOOD.replace("  - install.sh\n", "  - scripts/no_such_thing.py\n"))
        self.assertEqual((rc, r["reason"]), (1, "stale-reference"))

    def test_both_new_markers_accepted(self):
        rc, r = self.verdict(GOOD)
        self.assertFalse(any("does not exist" in e for e in r["errors"]), r["errors"])

    def test_vague_criterion_is_underspecified(self):
        rc, r = self.verdict(GOOD.replace("- [ ] the lint skill mentions the example", "- [ ] works"))
        self.assertEqual(r["reason"], "underspecified")

    def test_no_checkbox_is_underspecified(self):
        text = GOOD.replace("- [ ] the example script prints hello when run\n- [ ] the lint skill mentions the example",
                            "It should work well.")
        rc, r = self.verdict(text)
        self.assertEqual(r["reason"], "underspecified")

    def test_duplicate_across_stages(self):
        self.write(GOOD, stage="accepted")
        rc, r = self.verdict(GOOD)
        self.assertEqual(r["reason"], "duplicate")

    def test_breaking_change_escalates(self):
        rc, r = self.verdict(GOOD.replace("breaking_change: no", "breaking_change: yes"))
        self.assertEqual((rc, r["verdict"]), (3, "escalate"))

    def test_breaking_change_with_override_is_ready(self):
        rc, r = self.verdict(GOOD.replace("breaking_change: no", "breaking_change: yes")
                             .replace("owner_override: no", "owner_override: yes"))
        self.assertEqual(r["verdict"], "ready")

    def test_move_annotates_logs_and_refuses_final(self):
        p = self.write(GOOD)
        self.assertOk(self.py(FR, "move", p, "accepted", "--note", "looks good"))
        dest = self.queue / "accepted" / p.name
        self.assertFalse(p.exists())
        self.assertIn("## Evaluator Notes", dest.read_text())
        self.assertIn("looks good", dest.read_text())
        log = [json.loads(l) for l in (self.queue / "evaluator-log.jsonl").read_text().splitlines()]
        self.assertEqual((log[-1]["from"], log[-1]["to"]), ("new", "accepted"))
        self.assertOk(self.py(FR, "move", dest, "implemented", "--note", "gt 9.9.9, abc123"))
        final = self.queue / "implemented" / p.name
        self.assertIn("## Implementation", final.read_text())
        proc = self.py(FR, "move", final, "new", "--note", "reopen")
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(final.exists(), "a file in implemented/ must never move")

    def test_validate_all_reports_every_new_file(self):
        self.write(GOOD)
        self.write(GOOD.replace("2026-01-02-example-thing", "2026-01-02-second"), name="2026-01-02-second.md")
        proc = self.py(FR, "validate", "--all", self.queue, "--src", REPO, "--json")
        self.assertEqual(len(json.loads(proc.stdout)), 2)


if __name__ == "__main__":
    unittest.main()
