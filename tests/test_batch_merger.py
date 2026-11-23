
"""다중 MKV 그룹 순차 병합 테스트."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cctv_archive_manager.batch_merger import (
    GroupMergeError,
    merge_groups,
)
from cctv_archive_manager.merge_planner import (
    plan_merge_groups,
)
from cctv_archive_manager.merger import MergeResult


class TestBatchMerger(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        root = Path(self.temp.name)
        self.source = root / "source"
        self.output = root / "output"

        self.source.mkdir()
        self.output.mkdir()

        self.executable = root / "mkvmerge.exe"
        self.executable.write_bytes(b"FAKE")

        self.files = []

        for minute in range(0, 60, 10):
            path = self.source / (
                f"2026-09-03_00-{minute:02d}-00_000.mkv"
            )
            path.write_bytes(b"TEST")
            self.files.append(path)

        self.groups = plan_merge_groups(
            self.files, 3
        )

    def fake_merge(
        self,
        inputs,
        output,
        on_progress=None,
        mkvmerge=None,
    ):
        destination = Path(output)

        if on_progress:
            on_progress(0)
            on_progress(50)
            on_progress(100)

        destination.write_bytes(b"MERGED")

        return MergeResult(
            output=destination,
            input_count=len(inputs),
            output_bytes=6,
            return_code=0,
            log="TEST",
        )

    def test_three_groups_complete(self):
        with patch(
            "cctv_archive_manager.batch_merger."
            "merge_with_progress",
            side_effect=self.fake_merge,
        ):
            result = merge_groups(
                self.groups,
                self.output,
                mkvmerge=self.executable,
            )

        self.assertEqual(result.total_groups, 3)
        self.assertEqual(len(result.completed), 3)

        for group in self.groups:
            self.assertTrue(
                (self.output / group.output_name).is_file()
            )

    def test_progress_callback(self):
        events = []

        with patch(
            "cctv_archive_manager.batch_merger."
            "merge_with_progress",
            side_effect=self.fake_merge,
        ):
            merge_groups(
                self.groups,
                self.output,
                on_progress=lambda i, n, p: events.append(
                    (i, n, p)
                ),
                mkvmerge=self.executable,
            )

        self.assertIn((1, 3, 0), events)
        self.assertIn((2, 3, 50), events)
        self.assertIn((3, 3, 100), events)

    def test_existing_later_output_blocks_all(self):
        last = self.output / self.groups[-1].output_name
        last.write_bytes(b"EXISTING")

        with patch(
            "cctv_archive_manager.batch_merger."
            "merge_with_progress"
        ) as mocked:
            with self.assertRaises(FileExistsError):
                merge_groups(
                    self.groups,
                    self.output,
                    mkvmerge=self.executable,
                )

            mocked.assert_not_called()

        self.assertEqual(
            last.read_bytes(), b"EXISTING"
        )

    def test_failure_stops_remaining_groups(self):
        calls = []

        def fail_second(
            inputs,
            output,
            on_progress=None,
            mkvmerge=None,
        ):
            calls.append(Path(output).name)

            if len(calls) == 2:
                raise RuntimeError("SIMULATED FAILURE")

            return self.fake_merge(
                inputs, output, on_progress, mkvmerge
            )

        with patch(
            "cctv_archive_manager.batch_merger."
            "merge_with_progress",
            side_effect=fail_second,
        ):
            with self.assertRaises(
                GroupMergeError
            ) as context:
                merge_groups(
                    self.groups,
                    self.output,
                    mkvmerge=self.executable,
                )

        error = context.exception

        self.assertEqual(error.failed_group, 2)
        self.assertEqual(len(error.completed), 1)
        self.assertEqual(len(calls), 2)

        self.assertFalse(
            (self.output / self.groups[2].output_name).exists()
        )

    def test_empty_groups_rejected(self):
        with self.assertRaises(ValueError):
            merge_groups(
                [],
                self.output,
                mkvmerge=self.executable,
            )


if __name__ == "__main__":
    unittest.main()
