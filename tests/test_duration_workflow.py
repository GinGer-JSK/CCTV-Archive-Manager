"""Ensure duration planning and actual video execution stay separate."""
import unittest
from pathlib import Path
from unittest.mock import patch, sentinel

from cctv_archive_manager.duration_cache import DurationCacheResult
from cctv_archive_manager.duration_planner import DurationMergeGroup
from cctv_archive_manager.duration_probe import RecordingDuration
from cctv_archive_manager.duration_workflow import (
    DurationBatchPlan, execute_duration_batch, prepare_duration_batch,
)


class DurationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.records = tuple(
            RecordingDuration(Path(f"2026-09-03_00-{i:02d}-00_000.mkv"), ms)
            for i, ms in enumerate((600_000, 600_000, 100_000, 900_000))
        )
        self.cache = DurationCacheResult("2026-09-03", self.records, 4, 0, 0.01)

    def test_prepare_uses_cache_and_duration_planner(self):
        with patch("cctv_archive_manager.duration_workflow.cache_recording_durations", return_value=self.cache) as probe:
            plan = prepare_duration_batch("cache.db", "nvr", "2026-09-03", 2)
        probe.assert_called_once()
        self.assertEqual([len(g.files) for g in plan.groups], [2, 2])
        self.assertEqual(plan.total_duration_ms, self.cache.total_duration_ms)
        self.assertEqual([g.output_name for g in plan.groups], ["2026-09-03_part01.mkv", "2026-09-03_part02.mkv"])

    def test_planning_does_not_import_or_run_batch_merger(self):
        with patch("cctv_archive_manager.duration_workflow.cache_recording_durations", return_value=self.cache):
            with patch("cctv_archive_manager.duration_workflow.plan_duration_groups", wraps=__import__("cctv_archive_manager.duration_planner", fromlist=["plan_duration_groups"]).plan_duration_groups):
                plan = prepare_duration_batch("cache.db", "nvr", "2026-09-03", 2)
        self.assertEqual(len(plan.groups), 2)

    def test_execute_delegates_to_existing_merger(self):
        plan = DurationBatchPlan(self.cache, (
            DurationMergeGroup("2026-09-03", 1, tuple(r.path for r in self.records[:2]), "2026-09-03_part01.mkv", 1_200_000),
            DurationMergeGroup("2026-09-03", 2, tuple(r.path for r in self.records[2:]), "2026-09-03_part02.mkv", 1_000_000),
        ))
        with patch("cctv_archive_manager.batch_merger.merge_groups", return_value=sentinel.result) as merge:
            got = execute_duration_batch(plan, "results", on_progress=sentinel.callback, cancel_event=sentinel.cancel)
        self.assertIs(got, sentinel.result)
        self.assertEqual(len(merge.call_args.kwargs["groups"]), 2)
        self.assertEqual(merge.call_args.kwargs["output_dir"], Path("results"))
        self.assertIs(merge.call_args.kwargs["on_progress"], sentinel.callback)
        self.assertIs(merge.call_args.kwargs["cancel_event"], sentinel.cancel)

    def test_empty_plan_is_rejected_before_merger(self):
        with self.assertRaises(ValueError):
            execute_duration_batch(DurationBatchPlan(self.cache, ()), "out")

    def test_cache_failure_does_not_start_planning(self):
        with patch("cctv_archive_manager.duration_workflow.cache_recording_durations", side_effect=ValueError("No recordings")):
            with patch("cctv_archive_manager.duration_workflow.plan_duration_groups") as planner:
                with self.assertRaisesRegex(ValueError, "No recordings"):
                    prepare_duration_batch("cache.db", "nvr", "2026-09-03", 4)
        planner.assert_not_called()

    def test_invalid_plan_total_is_rejected(self):
        bad = DurationMergeGroup("2026-09-03", 1, (self.records[0].path, self.records[1].path), "out.mkv", 1)
        with patch("cctv_archive_manager.duration_workflow.cache_recording_durations", return_value=self.cache):
            with patch("cctv_archive_manager.duration_workflow.plan_duration_groups", return_value=[bad]):
                with self.assertRaisesRegex(RuntimeError, "lost input"):
                    prepare_duration_batch("cache.db", "nvr", "2026-09-03", 1)

    def test_wrong_duration_total_is_rejected(self):
        bad = DurationMergeGroup("2026-09-03", 1, tuple(r.path for r in self.records), "out.mkv", 1)
        with patch("cctv_archive_manager.duration_workflow.cache_recording_durations", return_value=self.cache):
            with patch("cctv_archive_manager.duration_workflow.plan_duration_groups", return_value=[bad]):
                with self.assertRaisesRegex(RuntimeError, "lost playback"):
                    prepare_duration_batch("cache.db", "nvr", "2026-09-03", 1)


if __name__ == "__main__":
    unittest.main()
