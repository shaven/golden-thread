"""gt_demote.py -- moving a note down the cost ladder without ever deleting it first.

Contract, and the ORDER is the contract:
  * WRITE the destination, VERIFY it by reading it back from disk, and only THEN replace the
    source with a pointer. A failure at any step leaves DUPLICATION -- the same content in two
    places, which a reader can see and resolve. gt_optimize's removed --apply failed by
    DELETION, which no diff brings back.
  * Without --apply, not a byte moves.
  * core-rules/ and Sources/ are never moved, by this or anything else that is a script:
    guard_protected_paths is a PreToolUse hook and never sees a script write a file.
  * An existing destination is a refusal. Merging two notes is a judgement a person makes.
"""
import hashlib
import io
import os
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from _harness import GT, load_module

SCRIPT = GT / "scripts" / "gt_demote.py"

BODY = ("# The Kelvin sign is not the letter K\n\n"
        "U+212A looks identical to K and is a different code point, which is why "
        "casefold() is used rather than lower().\n")


def digest(root):
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            h.update(os.path.relpath(p, root).encode())
            h.update(open(p, "rb").read())
    return h.hexdigest()


class DemoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gt-dem-"))
        self.vault = self.tmp / "vault"
        for d in ("global-memory", "Knowledge", "Sources",
                  "Projects/alpha/memory", "Projects/golden-thread/core-rules"):
            (self.vault / d).mkdir(parents=True)

    def write(self, rel, text=BODY):
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def run_dem(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), "--vault", str(self.vault)]
                              + list(args), capture_output=True, text=True)

    # -- nothing moves without --apply ----------------------------------------------------
    def test_without_apply_not_a_byte_moves(self):
        self.write("global-memory/kelvin.md")
        before = digest(self.vault)
        r = self.run_dem("--file", "global-memory/kelvin.md", "--project", "alpha")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(digest(self.vault), before)
        self.assertIn("Nothing has been moved", r.stdout)

    # -- the order that makes it safe -----------------------------------------------------
    def test_the_content_exists_at_the_destination_before_the_source_changes(self):
        self.write("global-memory/kelvin.md")
        r = self.run_dem("--file", "global-memory/kelvin.md", "--project", "alpha", "--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        dst = self.vault / "Projects/alpha/memory/kelvin.md"
        self.assertTrue(dst.is_file(), r.stdout)
        moved = dst.read_text(encoding="utf-8")
        self.assertIn("U+212A looks identical to K", moved, "the content must survive intact")
        self.assertIn("Demoted from global-memory/kelvin.md", moved, "provenance travels")

    def test_the_source_becomes_a_pointer_not_a_hole(self):
        self.write("global-memory/kelvin.md")
        self.run_dem("--file", "global-memory/kelvin.md", "--project", "alpha", "--apply")
        src = self.vault / "global-memory/kelvin.md"
        self.assertTrue(src.is_file(), "the source must still exist")
        text = src.read_text(encoding="utf-8")
        self.assertIn("Moved to", text)
        self.assertIn("kelvin", text)
        self.assertNotIn("U+212A looks identical", text, "content is moved, not duplicated")

    def test_nothing_is_lost_when_the_destination_cannot_be_written(self):
        """The failure mode that matters: if step 1 fails, step 3 must not happen."""
        self.write("global-memory/kelvin.md")
        kn = self.vault / "Knowledge"
        before = digest(self.vault)
        os.chmod(kn, 0o500)                       # read-only destination directory
        try:
            r = self.run_dem("--file", "global-memory/kelvin.md", "--to", "knowledge", "--apply")
            self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
            self.assertIn("nothing was removed", r.stderr)
            self.assertEqual(digest(self.vault), before, "the source must be untouched")
        finally:
            os.chmod(kn, 0o755)

    # -- refusals --------------------------------------------------------------------------
    def test_core_rules_is_never_moved(self):
        rel = "Projects/golden-thread/core-rules/core_x.md"
        self.write(rel)
        before = digest(self.vault)
        r = self.run_dem("--file", rel, "--to", "knowledge", "--apply")
        self.assertEqual(r.returncode, 1)
        self.assertIn("REFUSING", r.stdout)
        self.assertEqual(digest(self.vault), before)

    def test_sources_are_never_moved(self):
        self.write("Sources/raw.md")
        before = digest(self.vault)
        r = self.run_dem("--file", "Sources/raw.md", "--to", "knowledge", "--apply")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(digest(self.vault), before)

    def test_an_existing_destination_is_refused(self):
        self.write("global-memory/kelvin.md")
        self.write("Projects/alpha/memory/kelvin.md", "something else entirely\n")
        before = digest(self.vault)
        r = self.run_dem("--file", "global-memory/kelvin.md", "--project", "alpha", "--apply")
        self.assertEqual(r.returncode, 1)
        self.assertIn("already exists", r.stdout)
        self.assertEqual(digest(self.vault), before, "neither file may be touched")

    def test_knowledge_has_nowhere_cheaper_to_go(self):
        self.write("Knowledge/page.md")
        r = self.run_dem("--file", "Knowledge/page.md", "--apply")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cheapest tier", r.stdout)

    def test_a_traversal_is_refused(self):
        r = self.run_dem("--file", "../../etc/passwd", "--apply")
        self.assertEqual(r.returncode, 2)

    def test_a_file_outside_the_three_tiers_is_refused(self):
        self.write("Projects/alpha/research.md")
        r = self.run_dem("--file", "Projects/alpha/research.md", "--to", "knowledge", "--apply")
        self.assertEqual(r.returncode, 1)
        self.assertIn("in none of those", r.stdout)

    # -- the index -------------------------------------------------------------------------
    def test_index_rows_naming_the_file_are_reported(self):
        self.write("global-memory/kelvin.md")
        self.write("global-memory/MEMORY.md", "- [Kelvin](kelvin.md) — the sign\n")
        r = self.run_dem("--file", "global-memory/kelvin.md", "--project", "alpha", "--apply")
        self.assertIn("MEMORY.md", r.stdout)
        self.assertIn("row(s)", r.stdout)

    def test_vault_and_file_are_required(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--vault", r.stderr)

    # -- 2026-09-16 validation: nine ways the tool lost or leaked a note -------------------
    #
    # Each test below reproduces a defect found by running the tool for real on that date.
    # They are worth their runtime only because each one goes red again if its fix is
    # reverted -- verified by reverting each fix in turn.

    def markers_present(self, *markers):
        """Which of these strings can still be found in ANY file in the vault.

        The contract is not 'the destination exists', it is 'the content exists SOMEWHERE':
        a refusal that leaves the note at the source is a pass, a run that leaves it in
        neither place is the failure. So look for the text, not for a path.
        """
        found = set()
        for dirpath, _dirnames, filenames in os.walk(self.vault):
            for name in filenames:
                blob = open(os.path.join(dirpath, name), "rb").read()
                for m in markers:
                    if m.encode() in blob:
                        found.add(m)
        return found

    def test_two_runs_racing_to_one_destination_lose_neither_note(self):
        """`my-note.md` and `my_note.md` both normalise to `Knowledge/my note.md` -- `-` and
        `_` both become a space -- so two runs can collide on one destination.

        On 2026-09-16 both runs exited 0 and one note existed NOWHERE: each passed the
        os.path.exists() check while the destination was still absent, and the slower run's
        write landed on top of the faster one's AFTER that run had already replaced its source
        with a pointer. The fix is O_EXCL: the loser never writes at all.

        The collision is made deterministic by size rather than by luck. The window between
        the exists() check and the write is exactly the time spent reading and hashing the
        source, so the first run is given a source big enough for the second run's whole
        lifetime to fit inside it. If the machine is loaded enough that the two never overlap,
        the runs simply serialise and the assertion still holds -- this test can fail only
        when a note is genuinely lost, never because the timing drifted.
        """
        big = "# Big\n\nSLOW-RUN-MARKER\n" + "filler line to make the read take real time\n" * 2_800_000
        self.write("global-memory/my-note.md", big)
        self.write("global-memory/my_note.md", "# Small\n\nFAST-RUN-MARKER\n")
        cmd = [sys.executable, str(SCRIPT), "--vault", str(self.vault), "--to", "knowledge",
               "--apply", "--file"]
        slow = subprocess.Popen(cmd + ["global-memory/my-note.md"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.1)                       # let the slow run reach its source read
        fast = subprocess.Popen(cmd + ["global-memory/my_note.md"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertIsNone(slow.poll(), "the slow run must still be running when the fast one starts")
        fast_out = fast.communicate(timeout=120)
        slow_out = slow.communicate(timeout=120)
        report = "fast: %s\n%s\nslow: %s\n%s" % (fast.returncode, fast_out, slow.returncode, slow_out)
        self.assertEqual(self.markers_present("SLOW-RUN-MARKER", "FAST-RUN-MARKER"),
                         {"SLOW-RUN-MARKER", "FAST-RUN-MARKER"},
                         "a colliding run deleted a note that now exists nowhere\n" + report)

    def test_a_source_edited_after_it_was_read_is_not_replaced_by_a_pointer(self):
        """The source is re-read and hash-compared immediately before the pointer replaces it,
        because a copy taken seconds earlier is not evidence about what is on disk NOW: an edit
        arriving mid-run was silently destroyed by the pointer on 2026-09-16.

        The real timing cannot be driven from outside the process -- the window is a few
        milliseconds of local file I/O -- so this drives it from INSIDE, running main() in
        process and using render_destination as the seam: it is called after the body has been
        read and before the destination is written, which is precisely where a concurrent edit
        would land. That tests the re-check itself rather than an approximation of it.
        """
        mod = load_module(SCRIPT, "gt_demote_race")
        src = self.write("global-memory/kelvin.md")
        real_render = mod.render_destination

        def render_then_edit(*a, **kw):
            out = real_render(*a, **kw)
            src.write_text(BODY + "\nAN EDIT THAT ARRIVED MID-RUN\n", encoding="utf-8")
            return out

        mod.render_destination = render_then_edit
        try:
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = mod.main(["--vault", str(self.vault), "--file", "global-memory/kelvin.md",
                               "--project", "alpha", "--apply"])
        finally:
            mod.render_destination = real_render
        self.assertEqual(rc, 3, err.getvalue())
        self.assertIn("Nothing was removed", err.getvalue())
        self.assertIn("changed while this ran", err.getvalue())
        self.assertIn("AN EDIT THAT ARRIVED MID-RUN", src.read_text(encoding="utf-8"),
                      "the newer text must survive; a duplicate is the correct outcome here")

    def test_project_must_be_a_slug_and_never_a_path(self):
        """`--project ../../../ESCAPED` and `--project /tmp/x` both wrote the note OUTSIDE the
        vault and left the source pointing at a page that did not exist (2026-09-16). A slug
        names a directory under Projects/; it is not a path, so anything path-shaped is a
        refusal before a byte moves."""
        self.write("global-memory/kelvin.md")
        before = digest(self.tmp)             # the WHOLE temp tree: an escape lands outside the vault
        for bad in ("../../../ESCAPED", str(self.tmp / "ESCAPED"), "a/b"):
            r = self.run_dem("--file", "global-memory/kelvin.md", "--project", bad, "--apply")
            self.assertEqual(r.returncode, 1, "%r was not refused: %s" % (bad, r.stdout + r.stderr))
            self.assertIn("slug", r.stdout, r.stdout)
            self.assertEqual(digest(self.tmp), before,
                             "%r wrote something; nothing may be created anywhere" % bad)

    def test_a_symlinked_source_pointing_out_of_the_vault_is_refused(self):
        """A symlink in global-memory/ was followed out of the vault: the tool read the outside
        file and then OVERWROTE it with the pointer markdown (2026-09-16). The file it was
        never pointed at must come back byte for byte."""
        outside = self.tmp / "outside" / "not-a-note.md"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"# Someone else's file\n\nDO NOT TOUCH\n")
        before = outside.read_bytes()
        (self.vault / "global-memory" / "escape.md").symlink_to(outside)
        r = self.run_dem("--file", "global-memory/escape.md", "--to", "knowledge", "--apply")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("symlink", r.stdout)
        self.assertEqual(outside.read_bytes(), before, "a file outside the vault was rewritten")

    def test_a_symlinked_parent_directory_is_refused(self):
        """A symlinked PARENT escapes just as well as a symlinked file, and the containment
        check used to look only at the leaf: Projects/beta/memory -> core-rules/ made every
        Core rule reachable as an ordinary project note (2026-09-16)."""
        core = self.vault / "Projects/golden-thread/core-rules"
        (core / "core_x.md").write_text(BODY, encoding="utf-8")
        (self.vault / "Projects/beta").mkdir(parents=True)
        (self.vault / "Projects/beta/memory").symlink_to(core, target_is_directory=True)
        before = digest(self.vault)
        r = self.run_dem("--file", "Projects/beta/memory/core_x.md", "--to", "knowledge", "--apply")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(digest(self.vault), before)

    def test_protected_directories_are_matched_on_the_resolved_name_not_the_spelling(self):
        """`CORE-RULES/` and `sources/` walked straight past the refusal on a case-insensitive
        volume and a real Core rule was moved (2026-09-16): the check compared the caller's
        literal spelling. It now compares the RESOLVED path, casefolded and NFC-normalised."""
        if not (self.vault / "SOURCES").exists():
            self.skipTest("case-sensitive volume: these spellings name no file here")
        self.write("Sources/raw.md")
        self.write("Projects/golden-thread/core-rules/core_x.md")
        before = digest(self.vault)
        for bad in ("sources/raw.md", "SoUrCeS/raw.md",
                    "Projects/golden-thread/CORE-RULES/core_x.md"):
            r = self.run_dem("--file", bad, "--to", "knowledge", "--apply")
            self.assertEqual(r.returncode, 1, "%r was not refused: %s" % (bad, r.stdout + r.stderr))
            # The REASON is the assertion, not the exit code: an odd spelling also falls out of
            # classify() and is refused as "in none of those tiers", which is a refusal by
            # accident. What must hold is that the protection itself recognised the path.
            self.assertIn("is never moved by a script", r.stdout,
                          "%r was refused, but not as a protected path: %s" % (bad, r.stdout))
            self.assertEqual(digest(self.vault), before)

    def test_the_segment_comparison_casefolds_and_composes(self):
        """The Unicode half of the same defect, tested on the helper: no NFD spelling of
        `sources` or `core-rules` exists (both are pure ASCII), so the CLI cannot exercise
        composition on those two names. What must hold is that the comparison normalises at
        all -- casefold() over lower() is why U+212A, the Kelvin sign, cannot smuggle a name
        past a check, and NFC is why a decomposed accent cannot either."""
        mod = load_module(SCRIPT, "gt_demote_norm")
        self.assertEqual(mod._norm_seg("K"), "k", "casefold(), not lower()")
        self.assertEqual(mod._norm_seg("CORE-RULES"), "core-rules")
        self.assertEqual(mod._norm_seg(unicodedata.normalize("NFD", "Sourcés")),
                         unicodedata.normalize("NFC", "sourcés"))

    def test_the_destination_keeps_the_source_file_mode(self):
        """A note at 0600 arrived at 0644 (2026-09-16): the destination was created with the
        writer's own default mode, so demoting a private note published it to every reader of
        the vault.

        Both directions are asserted on purpose. 0600 alone would pass on a tool that merely
        creates everything at 0600 -- which is how the destination happens to be opened today,
        so that half of the test proves nothing by itself. The mode must be COPIED from the
        source, which only the 0644 case can show."""
        for mode, name in ((0o600, "private.md"), (0o644, "shared.md")):
            src = self.write("global-memory/%s" % name)
            os.chmod(src, mode)
            r = self.run_dem("--file", "global-memory/%s" % name, "--project", "alpha", "--apply")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            dst = self.vault / "Projects/alpha/memory" / name
            self.assertEqual(stat.S_IMODE(dst.stat().st_mode), mode,
                             "moving a note must not change who can read it")

    def test_crlf_line_endings_arrive_unconverted(self):
        """Reading in text mode without newline="" turned a CRLF note into LF (2026-09-16).
        That is byte-level corruption of someone's file, and it shows up as a whole-file diff
        in whatever tracks the vault.

        STILL RED as of 2026-09-16: the two READS in the move gained newline="" but the
        verify read in step 2 did not, so the bytes on disk (CRLF) are compared against the
        bytes that were written (CRLF) after the reader has already collapsed them to LF. The
        move aborts with exit 3. Nothing is lost -- the source is intact and the destination is
        a duplicate -- but a CRLF note cannot be demoted at all. Left failing rather than
        weakened, per the harness rule about tests that find a real defect."""
        body = "# CRLF\r\n\r\nthis note came from a Windows editor\r\n"
        self.write("global-memory/crlf.md", body)
        r = self.run_dem("--file", "global-memory/crlf.md", "--project", "alpha", "--apply")
        self.assertEqual(r.returncode, 0,
                         "a CRLF source aborts the move: the step-2 verify re-reads the "
                         "destination without newline=\"\", so it never matches what was "
                         "written.\n" + r.stdout + r.stderr)
        got = (self.vault / "Projects/alpha/memory/crlf.md").read_bytes()
        self.assertIn(body.encode(), got, "the original bytes must survive the move exactly")
        self.assertNotIn(b"editor\n", got.replace(b"\r\n", b""), "no CRLF was converted to LF")

    def test_a_source_that_is_not_utf8_is_refused_not_a_traceback(self):
        """A latin-1 note raised UnicodeDecodeError out of main() (2026-09-16): an unhandled
        traceback tells the caller nothing about whether the note survived. A tool that cannot
        read the bytes must say so and stop."""
        p = self.vault / "global-memory" / "latin1.md"
        p.write_bytes(b"# Caf\xe9\n\nnot valid UTF-8\n")
        before = digest(self.vault)
        r = self.run_dem("--file", "global-memory/latin1.md", "--project", "alpha", "--apply")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr, "a refusal, not a crash")
        self.assertIn("UTF-8", r.stderr)
        self.assertEqual(digest(self.vault), before)


if __name__ == "__main__":
    unittest.main()
