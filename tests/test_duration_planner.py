"""Unit tests for duration-aware, file-boundary-only grouping."""
import unittest
from dataclasses import dataclass
from pathlib import Path

from cctv_archive_manager.duration_planner import plan_duration_groups


@dataclass(frozen=True)
class Recording:
    path: Path
    duration_ms: int


def sample(durations, date="2026-09-03"):
    # Use a different valid millisecond timestamp per input.
    return [Recording(Path(f"{date}_{i // 3600000:02d}-{(i // 60000) % 60:02d}-{(i // 1000) % 60:02d}_{i % 1000:03d}.mkv"), d)
            for i, d in enumerate(durations)]


class DurationPlannerTests(unittest.TestCase):
    def test_143_equal_length_files_split_into_four(self):
        groups = plan_duration_groups(sample([600_000] * 143), 4)
        self.assertEqual([len(x.files) for x in groups], [36, 36, 36, 35])
        self.assertEqual(sum(x.total_duration_ms for x in groups), 143 * 600_000)
        self.assertEqual(groups[0].output_name, "2026-09-03_part01.mkv")

    def test_balance_duration_not_file_count(self):
        # 8 recordings, 2 groups; 4/4 by count is badly unbalanced.
        groups = plan_duration_groups(sample([1, 1, 1, 1, 10, 10, 1, 1]), 2)
        self.assertEqual([len(x.files) for x in groups], [5, 3])
        self.assertEqual([x.total_duration_ms for x in groups], [14, 12])

    def test_chronological_sort_and_no_missing_or_duplicate_items(self):
        recordings = sample([600, 100, 300, 200, 500, 400])
        groups = plan_duration_groups(list(reversed(recordings)), 2)
        flattened = [x for group in groups for x in group.files]
        self.assertEqual(flattened, [x.path for x in recordings])
        self.assertEqual(len(flattened), len(set(flattened)))

    def test_keeps_two_files_per_group(self):
        groups = plan_duration_groups(sample([1, 1, 1, 1, 1, 100]), 3)
        self.assertEqual([len(x.files) for x in groups], [2, 2, 2])

    def test_reject_too_many_groups(self):
        with self.assertRaises(ValueError):
            plan_duration_groups(sample([1, 1, 1]), 2)

    def test_reject_mixed_dates(self):
        data = sample([1, 2]) + sample([3, 4], "2026-09-04")
        with self.assertRaises(ValueError):
            plan_duration_groups(data, 2)

    def test_reject_invalid_or_duplicate_durations(self):
        for invalid in (0, -1, True, 1.1, "100"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                plan_duration_groups(sample([100, invalid]), 1)
        first = sample([100, 200])[0]
        with self.assertRaises(ValueError):
            plan_duration_groups([first, first], 1)

    def test_reject_invalid_filenames(self):
        for name in ("wrong.mkv", "2026-09-03_25-00-00_000.mkv"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                plan_duration_groups([Recording(Path(name), 10), Recording(Path("2026-09-03_00-00-00_000.mkv"), 10)], 1)

    def test_one_group_has_all_recordings(self):
        groups = plan_duration_groups(sample([3, 7, 11]), 1)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].total_duration_ms, 21)


if __name__ == "__main__":
    unittest.main()
