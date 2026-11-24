"""Regression tests for cancellation of sequential MKV group merging."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from cctv_archive_manager.batch_merger import (
    BatchMergeCancelled,
    merge_groups,
)
from cctv_archive_manager.merge_planner import plan_merge_groups
from cctv_archive_manager.merger import MergeResult
from cctv_archive_manager.progress_merger import MergeCancelled


class BatchCancellationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source = root / "source"
        self.output_dir = root / "output"
        source.mkdir()
        self.output_dir.mkdir()

        self.mkvmerge = root / "mkvmerge.exe"
        self.mkvmerge.write_bytes(b"FAKE")

        files = []
        for minute in range(0, 60, 10):
            file = source / f"2026-09-03_00-{minute:02d}-00_000.mkv"
            file.write_bytes(b"SOURCE")
            files.append(file)
        self.groups = plan_merge_groups(files, 3)
        self.event = threading.Event()

    def _success(self, *, inputs, output, on_progress, mkvmerge, **kwargs):
        output = Path(output)
        output.write_bytes(b"MERGED")
        on_progress(100)
        return MergeResult(
            output=output,
            input_count=len(inputs),
            output_bytes=6,
            return_code=0,
            log="TEST",
        )

    def test_cancel_before_first_group(self):
        self.event.set()
        with patch("cctv_archive_manager.batch_merger.merge_with_progress") as mocked:
            with self.assertRaises(BatchMergeCancelled) as caught:
                merge_groups(
                    self.groups, self.output_dir,
                    mkvmerge=self.mkvmerge,
                    cancel_event=self.event,
                )
            mocked.assert_not_called()
        error = caught.exception
        self.assertEqual(error.cancelled_group, 1)
        self.assertEqual(len(error.completed), 0)
        self.assertEqual(list(self.output_dir.glob("*.mkv")), [])

    def test_cancel_after_one_successful_group(self):
        calls = []

        def fake_merge(**kwargs):
            calls.append(Path(kwargs["output"]).name)
            result = self._success(**kwargs)
            self.event.set()
            return result

        with patch(
            "cctv_archive_manager.batch_merger.merge_with_progress",
            side_effect=fake_merge,
        ):
            with self.assertRaises(BatchMergeCancelled) as caught:
                merge_groups(
                    self.groups, self.output_dir,
                    mkvmerge=self.mkvmerge,
                    cancel_event=self.event,
                )

        error = caught.exception
        self.assertEqual(error.cancelled_group, 2)
        self.assertEqual(len(error.completed), 1)
        self.assertEqual(len(calls), 1)
        self.assertTrue((self.output_dir / self.groups[0].output_name).exists())
        self.assertFalse((self.output_dir / self.groups[1].output_name).exists())

    def test_cancel_while_second_group_is_running(self):
        calls = []

        def fake_merge(**kwargs):
            calls.append(Path(kwargs["output"]).name)
            if len(calls) == 1:
                return self._success(**kwargs)
            self.event.set()
            raise MergeCancelled("TEST CANCEL")

        with patch(
            "cctv_archive_manager.batch_merger.merge_with_progress",
            side_effect=fake_merge,
        ):
            with self.assertRaises(BatchMergeCancelled) as caught:
                merge_groups(
                    self.groups, self.output_dir,
                    mkvmerge=self.mkvmerge,
                    cancel_event=self.event,
                )

        error = caught.exception
        self.assertEqual(error.cancelled_group, 2)
        self.assertEqual(len(error.completed), 1)
        self.assertEqual(len(calls), 2)
        self.assertFalse((self.output_dir / self.groups[2].output_name).exists())

    def test_cancel_event_is_passed_to_single_merge_engine(self):
        seen_events = []

        def fake_merge(**kwargs):
            seen_events.append(kwargs.get("cancel_event"))
            return self._success(**kwargs)

        with patch(
            "cctv_archive_manager.batch_merger.merge_with_progress",
            side_effect=fake_merge,
        ):
            result = merge_groups(
                self.groups, self.output_dir,
                mkvmerge=self.mkvmerge,
                cancel_event=self.event,
            )

        self.assertEqual(len(result.completed), 3)
        self.assertEqual(seen_events, [self.event] * 3)

    def test_old_call_signature_does_not_pass_cancel_event(self):
        def fake_merge(*, inputs, output, on_progress, mkvmerge):
            return self._success(
                inputs=inputs,
                output=output,
                on_progress=on_progress,
                mkvmerge=mkvmerge,
            )

        with patch(
            "cctv_archive_manager.batch_merger.merge_with_progress",
            side_effect=fake_merge,
        ):
            result = merge_groups(
                self.groups, self.output_dir,
                mkvmerge=self.mkvmerge,
            )

        self.assertEqual(len(result.completed), 3)


if __name__ == "__main__":
    unittest.main()
