
"""MKVToolNix 병합 취소 기능 테스트."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from cctv_archive_manager.progress_merger import (
    MergeCancelled,
    merge_with_progress,
)


class TestMergeCancellation(unittest.TestCase):

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

    def test_cancel_before_start(self):
        """시작 전에 취소되면 프로세스를 실행하지 않는다."""

        event = threading.Event()
        event.set()

        with patch(
            "cctv_archive_manager.progress_merger."
            "subprocess.Popen"
        ) as mocked:
            with self.assertRaises(MergeCancelled):
                merge_with_progress(
                    self.inputs,
                    self.output,
                    mkvmerge=self.executable,
                    cancel_event=event,
                )

            mocked.assert_not_called()

        self.assertFalse(self.output.exists())
        self.assertFalse(self.lock.exists())

    def test_cancel_does_not_change_originals(self):
        """취소된 작업은 원본 파일을 변경하지 않는다."""

        originals = [
            file.read_bytes()
            for file in self.inputs
        ]

        event = threading.Event()
        event.set()

        with self.assertRaises(MergeCancelled):
            merge_with_progress(
                self.inputs,
                self.output,
                mkvmerge=self.executable,
                cancel_event=event,
            )

        for file, expected in zip(self.inputs, originals):
            self.assertEqual(
                file.read_bytes(),
                expected,
            )

    def test_existing_lock_preserved(self):
        """기존 잠금 파일은 취소 후에도 보존한다."""

        self.lock.write_text(
            "pid=12345\n",
            encoding="utf-8",
        )

        event = threading.Event()

        with self.assertRaises(FileExistsError):
            merge_with_progress(
                self.inputs,
                self.output,
                mkvmerge=self.executable,
                cancel_event=event,
            )

        self.assertTrue(self.lock.exists())
        self.assertIn(
            "pid=12345",
            self.lock.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
