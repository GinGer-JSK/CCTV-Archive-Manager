"""Step 5-6 resume manager: isolated local files, no NVR or real mkvmerge."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from cctv_archive_manager.daily_resume import (
    CONTROLLER_LOCK, MANIFEST_NAME, ResumeConflict,
    plan_cached_days, run_daily_resume,
)
from cctv_archive_manager.merge_planner import MergeGroup


class DailyResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        self.dest = self.base / 'dest'
        self.source.mkdir()
        self.dest.mkdir()
        self.groups = []
        for day in ('2026-09-03', '2026-09-04'):
            files = []
            for h in range(2):
                file = self.source / f'{day}_00-{h:02d}-00_000.mkv'
                file.write_bytes(b'fake input ' + bytes([h]))
                files.append(file)
            self.groups.append(MergeGroup(day, 1, tuple(files)))

    def merger(self, group, dest, cancel_event, cb):
        cb(50)
        dest.write_bytes(b'fake completed mkv')

    def test_two_dates_complete_and_resume_skips_both(self):
        calls = []
        def merge(group, output, cancel, cb):
            calls.append(group.date)
            self.merger(group, output, cancel, cb)
        first = run_daily_resume(self.groups, self.dest, merge_one=merge)
        self.assertEqual(len(first.completed), 2)
        self.assertEqual(calls, ['2026-09-03', '2026-09-04'])
        second = run_daily_resume(self.groups, self.dest, merge_one=merge)
        self.assertEqual(second.completed, ())
        self.assertEqual(len(second.skipped), 2)
        self.assertEqual(len(calls), 2)
        self.assertFalse((self.dest / CONTROLLER_LOCK).exists())

    def test_interrupted_after_first_checkpoint_resumes_second(self):
        calls = []
        def interrupted(group, output, cancel, cb):
            self.merger(group, output, cancel, cb)
            cancel.set()
            calls.append(group.date)
        cancel = threading.Event()
        first = run_daily_resume(self.groups, self.dest, merge_one=interrupted, cancel_event=cancel)
        self.assertTrue(first.cancelled)
        self.assertEqual(len(first.completed), 1)
        second = run_daily_resume(self.groups, self.dest, merge_one=self.merger)
        self.assertEqual(second.skipped, ('2026-09-03_part01.mkv',))
        self.assertEqual(second.completed, ('2026-09-04_part01.mkv',))

    def test_untracked_existing_output_is_never_skipped_or_overwritten(self):
        out = self.dest / self.groups[0].output_name
        out.write_bytes(b'old result')
        with self.assertRaisesRegex(ResumeConflict, 'untracked'):
            run_daily_resume(self.groups, self.dest, merge_one=self.merger)
        self.assertEqual(out.read_bytes(), b'old result')

    def test_partial_or_lock_prevents_merge_even_if_journal_completed(self):
        run_daily_resume(self.groups, self.dest, merge_one=self.merger)
        partial = self.dest / '2026-09-04_part01.partial.mkv'
        partial.write_bytes(b'partial')
        with self.assertRaisesRegex(ResumeConflict, 'partial or lock'):
            run_daily_resume(self.groups, self.dest, merge_one=self.merger)
        self.assertEqual(partial.read_bytes(), b'partial')

    def test_changed_output_blocks_resume(self):
        run_daily_resume(self.groups, self.dest, merge_one=self.merger)
        out = self.dest / self.groups[0].output_name
        out.write_bytes(b'changed')
        with self.assertRaisesRegex(ResumeConflict, 'completed file changed'):
            run_daily_resume(self.groups, self.dest, merge_one=self.merger)

    def test_changed_source_blocks_resume(self):
        run_daily_resume(self.groups, self.dest, merge_one=self.merger)
        self.groups[0].files[0].write_bytes(b'source changed')
        with self.assertRaisesRegex(ResumeConflict, 'plan changed'):
            run_daily_resume(self.groups, self.dest, merge_one=self.merger)

    def test_existing_controller_lock_is_preserved(self):
        lock = self.dest / CONTROLLER_LOCK
        lock.write_text('someone else')
        with self.assertRaisesRegex(ResumeConflict, 'controller lock'):
            run_daily_resume(self.groups, self.dest, merge_one=self.merger)
        self.assertEqual(lock.read_text(), 'someone else')

    def test_failed_merge_does_not_checkpoint_and_keeps_partial(self):
        def merge(group, dest, cancel, cb):
            dest.with_name(dest.stem + '.partial.mkv').write_bytes(b'incomplete')
            raise RuntimeError('test interruption')
        with self.assertRaisesRegex(RuntimeError, 'interruption'):
            run_daily_resume(self.groups, self.dest, merge_one=merge)
        journal = json.loads((self.dest / MANIFEST_NAME).read_text())
        self.assertEqual(journal['completed'], {})
        with self.assertRaises(ResumeConflict):
            run_daily_resume(self.groups, self.dest, merge_one=self.merger)

    def test_lock_of_pending_output_halts_preflight(self):
        lock = self.dest / (self.groups[1].output_name + '.lock')
        lock.write_text('protected')
        with self.assertRaisesRegex(ResumeConflict, 'partial or lock'):
            run_daily_resume(self.groups, self.dest, merge_one=self.merger)
        self.assertEqual(lock.read_text(), 'protected')
        self.assertFalse((self.dest / self.groups[0].output_name).exists())

    def test_source_change_after_first_completion_blocks_next(self):
        def change_source(group, dest, cancel, cb):
            self.merger(group, dest, cancel, cb)
            self.groups[1].files[0].write_bytes(b'changed during merge')
        with self.assertRaisesRegex(ResumeConflict, 'source changed'):
            run_daily_resume(self.groups, self.dest, merge_one=change_source)
        journal = json.loads((self.dest / MANIFEST_NAME).read_text())
        self.assertEqual(journal['completed'], {})

    def test_pre_cancel_does_not_launch_merger(self):
        cancel = threading.Event()
        cancel.set()
        def forbidden(*args):
            self.fail('should not merge')
        result = run_daily_resume(self.groups, self.dest, merge_one=forbidden, cancel_event=cancel)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.completed, ())

    def test_same_source_in_two_groups_rejected(self):
        duplicate = MergeGroup('2026-09-04', 2, self.groups[0].files)
        with self.assertRaisesRegex(ValueError, 'duplicate source'):
            run_daily_resume([self.groups[0], duplicate], self.dest, merge_one=self.merger)
        self.assertFalse((self.dest / MANIFEST_NAME).exists())

    def test_count_planning_for_explicit_days_only(self):
        with patch('cctv_archive_manager.catalog_reader.get_cached_files',
                   side_effect=[list(g.files) for g in self.groups]) as reader:
            planned = plan_cached_days('unused.db', self.source,
                                      ['2026-09-03', '2026-09-04'], 1)
        self.assertEqual(len(planned), 2)
        self.assertEqual(reader.call_count, 2)
        with self.assertRaises(ValueError):
            plan_cached_days('unused.db', self.source,
                             ['2026-09-04', '2026-09-03'], 1)

    def test_corrupt_manifest_rejected_without_merging(self):
        (self.dest / MANIFEST_NAME).write_text('{not json')
        with self.assertRaisesRegex(ResumeConflict, 'manifest unreadable'):
            run_daily_resume(self.groups, self.dest, merge_one=self.merger)


if __name__ == '__main__':
    unittest.main()
