"""Read-only artifact inspection tests."""

import tempfile
import unittest
from pathlib import Path

from cctv_archive_manager.recovery_inspector import (
    ArtifactStatus,
    inspect_merge_output,
)


class TestRecoveryInspector(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.output = self.folder / "2026-09-02_part01.mkv"
        self.partial = self.folder / "2026-09-02_part01.partial.mkv"
        self.lock = self.folder / "2026-09-02_part01.mkv.lock"

    def test_clean(self):
        result = inspect_merge_output(self.output)
        self.assertEqual(result.status, ArtifactStatus.CLEAN)
        self.assertIsNone(result.output_bytes)
        self.assertIsNone(result.partial_bytes)

    def test_completed(self):
        self.output.write_bytes(b"DONE")
        result = inspect_merge_output(self.output)
        self.assertEqual(result.status, ArtifactStatus.COMPLETE)
        self.assertEqual(result.output_bytes, 4)

    def test_partial_only(self):
        self.partial.write_bytes(b"")
        result = inspect_merge_output(self.output)
        self.assertEqual(result.status, ArtifactStatus.PARTIAL_ONLY)
        self.assertEqual(result.partial_bytes, 0)
        self.assertTrue(self.partial.exists())

    def test_lock_takes_precedence_and_is_preserved(self):
        self.partial.write_bytes(b"PARTIAL")
        self.lock.write_text("pid=123\nstarted_at=test\n", encoding="utf-8")
        result = inspect_merge_output(self.output)
        self.assertEqual(result.status, ArtifactStatus.LOCK_PRESENT)
        self.assertIn("pid=123", result.lock_contents)
        self.assertTrue(self.lock.exists())
        self.assertEqual(self.partial.read_bytes(), b"PARTIAL")

    def test_output_conflict(self):
        self.output.write_bytes(b"DONE")
        self.partial.write_bytes(b"INCOMPLETE")
        result = inspect_merge_output(self.output)
        self.assertEqual(result.status, ArtifactStatus.OUTPUT_CONFLICT)

    def test_reject_partial_as_output(self):
        with self.assertRaises(ValueError):
            inspect_merge_output(self.partial)


if __name__ == "__main__":
    unittest.main()
