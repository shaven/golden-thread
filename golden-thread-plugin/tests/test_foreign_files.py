"""dev/foreign_files.py -- what is in the publish destination that we did not publish.

Contract:
  * Print one destination-relative path per line for every file present in the
    destination and absent from the staged tree. Nothing else.
  * SOURCE.json is never foreign: the sync writes it into the destination, not the
    stage.
  * .DS_Store is never foreign; a cloud-synced folder grows them on its own.
  * A destination that does not exist yet is not an error — it is a first publish.
  * Exit 0 always. This reports; the human decides.
"""
import unittest

from _harness import Sandbox, REPO


TOOL = REPO / "dev" / "foreign_files.py"


class ForeignFiles(Sandbox):
    def setUp(self):
        super().setUp()
        self.dest = self.tmp / "dest"
        self.stage = self.tmp / "stage"
        self.dest.mkdir()
        self.stage.mkdir()

    def put(self, root, rel, body="x"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        return p

    def scan(self):
        p = self.py(TOOL, str(self.dest), str(self.stage))
        self.assertEqual(p.returncode, 0, "the scan must always exit 0: it reports only")
        return [l for l in p.stdout.splitlines() if l.strip()]

    def test_identical_trees_report_nothing(self):
        self.put(self.stage, "install.sh")
        self.put(self.dest, "install.sh")
        self.assertEqual(self.scan(), [])

    def test_the_2026_09_11_shape_is_reported(self):
        """A flat scripts/ and templates/ nobody's publisher wrote."""
        self.put(self.stage, "golden-thread/0.12.0/scripts/gt_adr.py")
        self.put(self.dest, "golden-thread/0.12.0/scripts/gt_adr.py")
        self.put(self.dest, "scripts/vault_init.py")
        self.put(self.dest, "templates/PROTOCOL.md")
        found = self.scan()
        self.assertIn("scripts/vault_init.py", found)
        self.assertIn("templates/PROTOCOL.md", found)
        self.assertNotIn("golden-thread/0.12.0/scripts/gt_adr.py", found,
                         "a file we published is not foreign")

    def test_source_json_is_never_foreign(self):
        """The sync writes it into the destination, so it is never in the stage."""
        self.put(self.dest, "SOURCE.json", '{"commit": "abc"}')
        self.assertEqual(self.scan(), [])

    def test_ds_store_is_ignored(self):
        self.put(self.dest, ".DS_Store")
        self.put(self.dest, "sub/.DS_Store")
        self.assertEqual(self.scan(), [],
                         "a cloud-synced folder grows .DS_Store on its own")

    def test_a_file_we_changed_is_not_foreign(self):
        self.put(self.stage, "README.md", "new text")
        self.put(self.dest, "README.md", "old text")
        self.assertEqual(self.scan(), [],
                         "differing CONTENT is an update, not a foreign writer")

    def test_missing_destination_is_a_first_publish(self):
        self.dest.rmdir()
        self.assertEqual(self.scan(), [])

    def test_paths_are_relative_to_the_destination(self):
        """An absolute path would be unreadable in the sync's output."""
        self.put(self.dest, "deep/nested/stray.py")
        self.assertEqual(self.scan(), ["deep/nested/stray.py"])

    def test_usage_error_is_exit_2(self):
        p = self.py(TOOL, str(self.dest))
        self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
