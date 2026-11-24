"""Multi-day GUI worker regression tests. No actual MKV merge or DB writes."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cctv_archive_manager.daily_gui import (
    DailyExecuteWorker, DailyPlanWorker, selected_cached_dates,
)


class DailyGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / 'source'
        self.output = root / 'output'
        self.source.mkdir()
        self.output.mkdir()
        self.db = root / 'catalog.sqlite3'
        self.db.write_bytes(b'not a real database')
        self.days = [SimpleNamespace(date='2026-09-03'),
                     SimpleNamespace(date='2026-09-04'),
                     SimpleNamespace(date='2026-09-10')]
        self.group = SimpleNamespace(date='2026-09-03', output_name='2026-09-03_part01.mkv',
                                     files=(self.source / 'a.mkv', self.source / 'b.mkv'))

    def test_date_range_uses_existing_cached_dates_only(self):
        with patch('cctv_archive_manager.daily_gui.get_catalog_snapshot',
                   return_value=SimpleNamespace(days=self.days)) as read:
            dates = selected_cached_dates(self.db, self.source, '2026-09-03', '2026-09-05')
        self.assertEqual(dates, ('2026-09-03', '2026-09-04'))
        read.assert_called_once()

    def test_more_than_seven_calendar_days_blocked_before_db_read(self):
        with patch('cctv_archive_manager.daily_gui.get_catalog_snapshot') as read:
            with self.assertRaisesRegex(ValueError, '최대 7일'):
                selected_cached_dates(self.db, self.source, '2026-09-03', '2026-09-10')
        read.assert_not_called()

    def test_reversed_range_blocked(self):
        with self.assertRaises(ValueError):
            selected_cached_dates(self.db, self.source, '2026-09-05', '2026-09-03')

    def test_missing_day_range_blocked(self):
        with patch('cctv_archive_manager.daily_gui.get_catalog_snapshot',
                   return_value=SimpleNamespace(days=self.days)):
            with self.assertRaisesRegex(ValueError, '녹화 기록'):
                selected_cached_dates(self.db, self.source, '2026-09-05', '2026-09-06')

    def test_plan_worker_preserves_qthread_start_method(self):
        """Date range must not shadow QThread.start() in real GUI usage."""
        worker = DailyPlanWorker(self.source, self.db, self.output,
                                 '2026-09-03', '2026-09-04', 4, 'count')
        self.assertTrue(callable(worker.start))
        self.assertEqual(worker.start_date, '2026-09-03')
        self.assertEqual(worker.end_date, '2026-09-04')

    def test_preview_only_prepares_plan_not_merge(self):
        worker = DailyPlanWorker(self.source, self.db, self.output,
                                 '2026-09-03', '2026-09-03', 1, 'count')
        results, errors = [], []
        worker.ready.connect(results.append)
        worker.failed.connect(errors.append)
        with patch('cctv_archive_manager.daily_gui.selected_cached_dates',
                   return_value=('2026-09-03',)), \
             patch('cctv_archive_manager.daily_gui.plan_cached_days',
                   return_value=(self.group,)) as plan, \
             patch('cctv_archive_manager.daily_gui.run_daily_resume') as merge:
            worker.run()
        self.assertFalse(errors, errors)
        self.assertEqual(results[0][0], (self.group,))
        plan.assert_called_once_with(self.db, self.source, ('2026-09-03',), 1, mode='count')
        merge.assert_not_called()

    def test_output_inside_source_rejected_without_planning(self):
        worker = DailyPlanWorker(self.source, self.db, self.source,
                                 '2026-09-03', '2026-09-03', 1, 'count')
        errors = []
        worker.failed.connect(errors.append)
        with patch('cctv_archive_manager.daily_gui.plan_cached_days') as plan:
            worker.run()
        self.assertTrue(any('원본 폴더 안' in e for e in errors), errors)
        plan.assert_not_called()

    def test_limit_28_output_groups_before_planning(self):
        worker = DailyPlanWorker(self.source, self.db, self.output,
                                 '2026-09-03', '2026-09-10', 5, 'duration')
        errors = []
        worker.failed.connect(errors.append)
        with patch('cctv_archive_manager.daily_gui.selected_cached_dates',
                   return_value=tuple(f'2026-09-{i:02d}' for i in range(3, 10))), \
             patch('cctv_archive_manager.daily_gui.plan_cached_days') as plan:
            worker.run()
        self.assertTrue(any('최대 28개' in e for e in errors), errors)
        plan.assert_not_called()

    def test_execute_passes_cancel_event_and_reports_result(self):
        worker = DailyExecuteWorker((self.group,), self.output)
        results, errors = [], []
        worker.ready.connect(results.append)
        worker.failed.connect(errors.append)
        expected = SimpleNamespace(completed=('2026-09-03_part01.mkv',),
                                   skipped=(), total=1, cancelled=False)
        with patch('cctv_archive_manager.daily_gui.run_daily_resume', return_value=expected) as execute:
            worker.run()
        self.assertFalse(errors, errors)
        self.assertIs(results[0][0], expected)
        self.assertIs(execute.call_args.kwargs['cancel_event'], worker.cancel_event)
        self.assertEqual(execute.call_args.args, ((self.group,), self.output))


if __name__ == '__main__':
    unittest.main()
