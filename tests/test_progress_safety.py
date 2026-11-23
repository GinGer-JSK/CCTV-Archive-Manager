
"""진행률 병합 엔진의 잠금 파일 안전성 테스트."""

import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from cctv_archive_manager.progress_merger import (
    _create_lock,
    merge_with_progress,
)


class TestProgressSafety(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        root = Path(self.temp.name)

        self.source = root / "source"
        self.output_dir = root / "output"

        self.source.mkdir()
        self.output_dir.mkdir()

        self.inputs = [
            self.source / "2026-09-02_00-00-00_000.mkv",
            self.source / "2026-09-02_00-10-00_000.mkv",
        ]

        for file in self.inputs:
            file.write_bytes(b"TEST")

        self.output = (
            self.output_dir / "2026-09-02_part01.mkv"
        )

        self.executable = root / "mkvmerge.exe"
        self.executable.write_bytes(b"FAKE")

        self.lock = self.output.with_name(
            self.output.name + ".lock"
        )

    def test_lock_contains_process_information(self):
        lock = _create_lock(self.output)

        try:
            contents = lock.read_text(
                encoding="utf-8"
            )

            self.assertIn("pid=", contents)
            self.assertIn("started_at=", contents)
            self.assertIn("output=", contents)

        finally:
            lock.unlink(missing_ok=True)

    def test_duplicate_lock_is_rejected(self):
        self.lock.write_text(
            "pid=12345\nstarted_at=TEST\n",
            encoding="utf-8",
        )

        with self.assertRaises(FileExistsError):
            _create_lock(self.output)

        self.assertTrue(self.lock.exists())

    def test_lock_cleanup_after_launch_failure(self):
        with patch(
            "cctv_archive_manager.progress_merger."
            "subprocess.Popen",
            side_effect=OSError("TEST PROCESS ERROR"),
        ):
            with self.assertRaises(OSError):
                merge_with_progress(
                    self.inputs,
                    self.output,
                    mkvmerge=self.executable,
                )

        self.assertFalse(self.lock.exists())
        self.assertFalse(self.output.exists())

    def test_existing_partial_is_preserved(self):
        partial = self.output.with_name(
            self.output.stem + ".partial.mkv"
        )
        partial.write_bytes(b"KEEP")

        with self.assertRaises(FileExistsError):
            merge_with_progress(
                self.inputs,
                self.output,
                mkvmerge=self.executable,
            )

        self.assertEqual(
            partial.read_bytes(),
            b"KEEP",
        )
        self.assertFalse(self.lock.exists())


if __name__ == "__main__":
    unittest.main()
