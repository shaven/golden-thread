"""gt-flow --redact: hash width and per-render salt, asserted directly (gt 0.18.0).

Request 2026-09-23-flow-redact-hash-collides. A redacted name was sha256(salt + value)
truncated to 4 hex characters, so two renders of 8 names shared a truncated hash about
once in 1,024 runs, and the old test blamed the salt for it. These tests pin:

  * the minimum emitted width is 6 hex characters;
  * within one render every occurrence of a name maps to one hash and no two names share one;
  * two renders produce different name-to-hash MAPPINGS (the property a fresh salt means);
  * a forced collision is resolved by the widening loop, which terminates -- including when
    the whole digest collides;
  * statistically: 20 names, 200 renders, no mapping ever repeats, and no render ever maps
    two names to one hash.
"""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _harness import REPO, PYTHON, load_module, latest_version_dir

FLOW = latest_version_dir(REPO / "golden-thread-flow")
SCRIPT = FLOW / "scripts" / "gt_flow.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_flow_module as tfm  # noqa: E402  (fixture reuse)

NAMES = ["Projects/name-%02d/research.md" % i for i in range(20)]
HASH = re.compile(r"^[a-z]-([0-9a-f]+)(?:-\d+)?$")


def gt_flow():
    return load_module(SCRIPT, "gt_flow_width_under_test")


class RedactWidth(unittest.TestCase):
    def setUp(self):
        self.g = gt_flow()

    def test_minimum_width_is_six(self):
        self.assertGreaterEqual(self.g.MIN_HASH_HEX, 6)
        n = self.g.Namer(True)
        for v in NAMES:
            h = HASH.match(n("f", v))
            self.assertIsNotNone(h)
            self.assertGreaterEqual(len(h.group(1)), 6, n("f", v))

    def test_stable_and_distinct_within_one_render(self):
        n = self.g.Namer(True)
        first = {v: n("f", v) for v in NAMES}
        again = {v: n("f", v) for v in reversed(NAMES)}
        self.assertEqual(first, again, "a name must map to one hash within a render")
        self.assertEqual(len(set(first.values())), len(NAMES), "two names share one hash")

    def test_unredacted_names_pass_through(self):
        n = self.g.Namer(False)
        self.assertEqual(n("f", NAMES[0]), NAMES[0])
        self.assertIsNone(n("f", None))

    def test_forced_prefix_collision_widens_and_terminates(self):
        n = self.g.Namer(True)
        fake = {"a": "abcdef01" + "0" * 56, "b": "abcdef02" + "1" * 56}
        n._digest = lambda prefix, value: fake[value]
        ha, hb = n("f", "a"), n("f", "b")
        self.assertEqual(ha, "f-abcdef")
        self.assertNotEqual(ha, hb)
        self.assertEqual(hb, "f-abcdef0", "widened by one character, the least that separates them")
        # a third name colliding with both widens past both
        fake["c"] = "abcdef0" + "2" * 57
        self.assertEqual(n("f", "c"), "f-abcdef02")

    def test_full_digest_collision_still_terminates(self):
        n = self.g.Namer(True)
        n._digest = lambda prefix, value: "f" * 64
        got = [n("f", v) for v in ("a", "b", "c")]
        self.assertEqual(len(set(got)), 3, got)

    def test_statistical_twenty_names_two_hundred_renders(self):
        # At 6 hex characters a shared truncated hash between two 20-name renders is about
        # 1 in 42,000; a whole MAPPING repeating needs every name to coincide, which only a
        # shared salt produces. So this is stable, and a failure here is about the salt.
        mappings = set()
        for _ in range(200):
            n = self.g.Namer(True)
            m = tuple(n("f", v) for v in NAMES)
            self.assertEqual(len(set(m)), len(NAMES), "one render showed two names under one hash")
            mappings.add(m)
        self.assertEqual(len(mappings), 200,
                         "a whole name-to-hash mapping repeated across renders: the salt is "
                         "not fresh per render")


class RedactRenderMapping(unittest.TestCase):
    """End to end: two `--redact` renders of one vault, mappings extracted and compared."""

    def render(self, vault, out):
        p = subprocess.run([PYTHON, str(SCRIPT), "render", "--vault", str(vault), "--out",
                            str(out), "--redact"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        page = out.read_text(encoding="utf-8")
        return json.loads(re.search(r'id="flow-data">(.*?)</script>', page, re.S).group(1))

    def mapping(self, events, data):
        """original value -> redacted value, pairing events by (ts, kind) order."""
        key = lambda e: (e["ts"], e["kind"])  # noqa: E731
        orig = sorted(events, key=key)
        red = sorted(data["events"], key=key)
        self.assertEqual(len(orig), len(red))
        m = {}
        for a, b in zip(orig, red):
            for field in ("from", "to", "session"):
                x, y = a.get(field), b.get(field)
                if x and y:
                    self.assertEqual(m.setdefault((field[0] if field != "session" else "s", x),
                                                  y), y, "one name, two hashes in one render")
        return m

    def test_two_renders_differ_in_mapping_and_each_is_injective(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            events = tfm.fixture_events()
            vault = tfm.write_vault(td, events)
            m1 = self.mapping(events, self.render(vault, td / "a.html"))
            m2 = self.mapping(events, self.render(vault, td / "b.html"))
        self.assertTrue(m1, "nothing redacted was compared -- the test proves nothing")
        for m in (m1, m2):
            by_hash = {}
            for (field, name), h in m.items():
                if field in ("f", "t"):
                    field = "path"
                prev = by_hash.setdefault(h, name)
                self.assertEqual(prev, name, "two distinct names under one hash %s" % h)
        self.assertNotEqual(m1, m2, "the name-to-hash mapping is identical across renders: "
                                    "the salt is not fresh per render")
        moved = sum(1 for k in m1 if m1[k] != m2.get(k))
        self.assertEqual(moved, len(m1),
                         "%d of %d names kept their hash across renders. With a fresh salt "
                         "each name's 6-hex hash coincides by chance about 1 in 16.7 million; "
                         "if only some names stayed, suspect a truncation collision, if all "
                         "did, suspect the salt" % (len(m1) - moved, len(m1)))


if __name__ == "__main__":
    unittest.main()
