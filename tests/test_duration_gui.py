"""GUI A/B planning integration regressions; no actual merge is launched."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cctv_archive_manager.gui import PlanWorker, format_duration_ms


class DurationGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / 'source'
        self.output = root / 'output'
        self.source.mkdir()
        self.output.mkdir()
        self.database = root / 'catalog.db'
        self.date = '2026-09-03'
        self.group = SimpleNamespace(
            output_name='2026-09-03_part01.mkv',
            files=(self.source / 'a.mkv', self.source / 'b.mkv'),
            total_duration_ms=1_200_000,
        )

    def worker(self, use_cache=True, mode='duration'):
        return PlanWorker(self.source, self.database, self.date, 1,
                          self.output, use_cache, mode)

    def test_time_format(self):
        self.assertEqual(format_duration_ms(3_613_200), '01:00:13')

    def test_duration_mode_requires_sqlite_list(self):
        worker = self.worker(use_cache=False)
        errors = []
        worker.error_occurred.connect(errors.append)
        worker.run()
        self.assertTrue(any('SQLite' in message for message in errors))

    def test_duration_mode_rejects_source_key_mismatch_before_cache_write(self):
        worker = self.worker()
        errors = []
        worker.error_occurred.connect(errors.append)
        with patch('cctv_archive_manager.gui.get_cached_files', return_value=[]) as lookup, \
             patch('cctv_archive_manager.gui.prepare_duration_batch') as prepare:
            worker.run()
        lookup.assert_called_once()
        prepare.assert_not_called()
        self.assertTrue(any('경로' in message for message in errors), errors)

    def test_duration_plan_is_delivered_without_merging(self):
        worker = self.worker()
        results, errors = [], []
        worker.result_ready.connect(results.append)
        worker.error_occurred.connect(errors.append)
        with patch('cctv_archive_manager.gui.get_cached_files', return_value=list(self.group.files)), \
             patch('cctv_archive_manager.gui.prepare_duration_batch',
                   return_value=SimpleNamespace(groups=(self.group,))) as prepare:
            worker.run()
        self.assertFalse(errors, errors)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], [self.group])
        prepare.assert_called_once_with(self.database, self.source, self.date, 1)

    def test_existing_lock_blocks_plan(self):
        (self.output / (self.group.output_name + '.lock')).write_text('busy')
        worker = self.worker()
        errors = []
        worker.error_occurred.connect(errors.append)
        with patch('cctv_archive_manager.gui.get_cached_files', return_value=list(self.group.files)), \
             patch('cctv_archive_manager.gui.prepare_duration_batch',
                   return_value=SimpleNamespace(groups=(self.group,))):
            worker.run()
        self.assertTrue(any('잠금' in message for message in errors), errors)


if __name__ == '__main__':
    unittest.main()
