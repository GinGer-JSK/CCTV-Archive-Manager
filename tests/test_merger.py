
"""MKV 병합 엔진의 사전 검증 테스트."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cctv_archive_manager.merger import (
    merge_recordings,
    prepare_merge,
)


class TestMerger(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        root = Path(self.temp.name)

        self.source = root / "source"
        self.output = root / "output"

        self.source.mkdir()
        self.output.mkdir()

        self.executable = root / "mkvmerge.exe"
        self.executable.write_bytes(b"test")

        self.first = self.source / (
            "2026-09-02_00-00-00_000.mkv"
        )
        self.second = self.source / (
            "2026-09-02_00-10-00_000.mkv"
        )

        self.first.write_bytes(b"first")
        self.second.write_bytes(b"second")

        self.destination = self.output / "merged.mkv"

    def test_command_sorted_by_time(self):
        command, _ = prepare_merge(
            [self.second, self.first],
            self.destination,
            self.executable,
        )

        self.assertEqual(command[3], str(self.first))
        self.assertEqual(command[4], "+")
        self.assertEqual(command[5], str(self.second))

    def test_reject_existing_output(self):
        self.destination.write_bytes(b"existing")

        with self.assertRaises(FileExistsError):
            prepare_merge(
                [self.first, self.second],
                self.destination,
                self.executable,
            )

    def test_reject_output_in_source(self):
        with self.assertRaises(ValueError):
            prepare_merge(
                [self.first, self.second],
                self.source / "merged.mkv",
                self.executable,
            )

    def test_reject_duplicate_input(self):
        with self.assertRaises(ValueError):
            prepare_merge(
                [self.first, self.first],
                self.destination,
                self.executable,
            )

    def test_reject_single_input(self):
        with self.assertRaises(ValueError):
            prepare_merge(
                [self.first],
                self.destination,
                self.executable,
            )

    @patch("cctv_archive_manager.merger.subprocess.run")
    def test_successful_merge(self, mock_run):
        def fake_run(command, **kwargs):
            Path(command[2]).write_bytes(b"merged-video")

            return type("Result", (), {
                "returncode": 0,
                "stdout": "Success",
                "stderr": "",
            })()

        mock_run.side_effect = fake_run

        result = merge_recordings(
            [self.second, self.first],
            self.destination,
            self.executable,
        )

        self.assertEqual(result.input_count, 2)
        self.assertEqual(result.output_bytes, 12)
        self.assertTrue(self.destination.is_file())
        self.assertTrue(self.first.is_file())
        self.assertTrue(self.second.is_file())


if __name__ == "__main__":
    unittest.main()
