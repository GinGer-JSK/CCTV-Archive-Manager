"""Step 16: fail-closed, read-only production output diagnosis."""
import json
import tempfile
import unittest
from pathlib import Path

from cctv_archive_manager.recovery_diagnosis import inspect_daily_output, format_diagnosis


class RecoveryDiagnosisTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.output = ('2026-09-03_part01.mkv',)

    def check_unchanged(self, fn):
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}
        result = fn()
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}
        self.assertEqual(before, after)
        return result

    def test_empty_folder_is_advisory_not_recovery_approval(self):
        report = inspect_daily_output(self.root, self.output)
        self.assertIn('READ ONLY', report.status)
        self.assertFalse(report.can_auto_recover)

    def test_controller_lock_blocks(self):
        (self.root / '.cctv-daily-resume.lock').write_text('pid=123\n')
        report = self.check_unchanged(lambda: inspect_daily_output(self.root, self.output))
        self.assertTrue(report.controller_lock)
        self.assertIn('BLOCKED', report.status)

    def test_partial_and_merge_lock_block(self):
        (self.root / '2026-09-03_part01.partial.mkv').write_bytes(b'partial')
        (self.root / '2026-09-03_part01.mkv.lock').write_text('test')
        report = self.check_unchanged(lambda: inspect_daily_output(self.root, self.output))
        self.assertEqual(len(report.partials), 1)
        self.assertEqual(len(report.merge_locks), 1)
        self.assertIn('BLOCKED', report.status)

    def test_manifest_structure_readonly(self):
        (self.root / '.cctv-daily-resume.json').write_text(json.dumps({
            'version': 1, 'signature': 'digest', 'completed': {self.output[0]: {'bytes': 30}}
        }))
        report = self.check_unchanged(lambda: inspect_daily_output(self.root, self.output))
        self.assertEqual(report.completed, self.output)
        self.assertFalse(report.can_auto_recover)
        self.assertIn('SHA-256', format_diagnosis(report))

    def test_invalid_manifest_fail_closed(self):
        (self.root / '.cctv-daily-resume.json').write_text('{broken')
        report = inspect_daily_output(self.root, self.output)
        self.assertIn('BLOCKED', report.status)

    def test_manifest_tmp_blocks(self):
        (self.root / '.cctv-daily-resume.json.tmp').write_text('interrupted')
        report = inspect_daily_output(self.root, self.output)
        self.assertIn('.tmp', report.unexpected[0])
        self.assertIn('BLOCKED', report.status)

    def test_unknown_completed_output_blocks(self):
        (self.root / 'different.mkv').write_bytes(b'junk')
        report = inspect_daily_output(self.root, self.output)
        self.assertIn('different.mkv', report.unexpected)
        self.assertIn('BLOCKED', report.status)

    def test_unplanned_is_advisory(self):
        report = inspect_daily_output(self.root)
        self.assertTrue(any('미리보기 계획' in x for x in report.messages))
        self.assertFalse(report.can_auto_recover)

    def test_duplicate_and_invalid_output_rejected(self):
        with self.assertRaises(ValueError):
            inspect_daily_output(self.root, ('x.mkv', 'X.mkv'))
        with self.assertRaises(ValueError):
            inspect_daily_output(self.root, ('../x.mkv',))

    def test_absent_directory_rejected(self):
        with self.assertRaises(NotADirectoryError):
            inspect_daily_output(self.root / 'does-not-exist')


if __name__ == '__main__':
    unittest.main()
