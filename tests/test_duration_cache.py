"""Regression tests for the additive, local-only MKV duration cache."""
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from cctv_archive_manager.duration_cache import (
    ensure_duration_columns, cache_recording_durations,
)
from cctv_archive_manager.duration_probe import RecordingDuration


class DurationCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source = root / 'nvr'
        self.source.mkdir()
        self.db = root / 'catalog.db'
        self.paths = [self.source / f'2026-09-03_00-{n:02d}-00_000.mkv'
                      for n in (0, 10)]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('''CREATE TABLE recordings (
                path TEXT PRIMARY KEY, source TEXT NOT NULL,
                filename TEXT NOT NULL, recorded_at TEXT NOT NULL,
                record_date TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL, last_seen_at TEXT NOT NULL)''')
            for p in self.paths:
                p.write_bytes(b'original-mkv-data')
                s = p.stat()
                conn.execute('''INSERT INTO recordings VALUES (?,?,?,?,?,?,?,?)''',
                    (str(p), str(self.source.resolve()), p.name,
                     p.name[:10] + 'T00:00:00', '2026-09-03',
                     s.st_size, s.st_mtime_ns, 'test'))

    def _probe(self, value=600_000):
        return Mock(side_effect=lambda path: RecordingDuration(path, value))

    def test_migration_is_idempotent_and_preserves_data(self):
        ensure_duration_columns(self.db)
        ensure_duration_columns(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            names = [r[1] for r in conn.execute('PRAGMA table_info(recordings)')]
            count = conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0]
        self.assertTrue({'duration_ms', 'duration_size_bytes',
                         'duration_modified_ns'} <= set(names))
        self.assertEqual(count, 2)

    def test_missing_db_does_not_create_new_file(self):
        path = self.db.parent / 'absent.db'
        with self.assertRaises(FileNotFoundError):
            ensure_duration_columns(path)
        self.assertFalse(path.exists())

    def test_missing_table_rejected(self):
        self.db.unlink()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            pass
        with self.assertRaises(ValueError):
            ensure_duration_columns(self.db)

    def test_first_run_probes_and_second_run_uses_cache(self):
        probe = self._probe()
        initial = cache_recording_durations(self.db, self.source, '2026-09-03', probe=probe)
        self.assertEqual((initial.probed, initial.cache_hits), (2, 0))
        self.assertEqual(initial.total_duration_ms, 1_200_000)
        again = cache_recording_durations(self.db, self.source, '2026-09-03', probe=probe)
        self.assertEqual((again.probed, again.cache_hits), (0, 2))
        self.assertEqual(probe.call_count, 2)

    def test_modified_source_invalidates_only_one_file(self):
        initial_probe = self._probe()
        cache_recording_durations(self.db, self.source, '2026-09-03', probe=initial_probe)
        old = self.paths[0].stat().st_mtime_ns
        import os
        os.utime(self.paths[0], ns=(old + 2_000_000_000, old + 2_000_000_000))
        new_probe = self._probe(601_000)
        result = cache_recording_durations(self.db, self.source, '2026-09-03', probe=new_probe)
        self.assertEqual((result.probed, result.cache_hits), (1, 1))
        self.assertEqual(new_probe.call_count, 1)
        self.assertEqual(result.records[0].duration_ms, 601_000)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute('SELECT modified_ns FROM recordings WHERE path=?',
                              (str(self.paths[0]),)).fetchone()[0], self.paths[0].stat().st_mtime_ns)

    def test_size_change_invalidates_cache(self):
        cache_recording_durations(self.db, self.source, '2026-09-03', probe=self._probe())
        with self.paths[1].open('ab') as f:
            f.write(b'x')
        fresh_probe = self._probe(599_000)
        result = cache_recording_durations(self.db, self.source, '2026-09-03', probe=fresh_probe)
        self.assertEqual((result.probed, result.cache_hits), (1, 1))

    def test_partial_success_survives_failed_probe(self):
        def failing_probe(path):
            if path == self.paths[1]:
                raise RuntimeError('probe failure')
            return RecordingDuration(path, 600_000)
        with self.assertRaisesRegex(RuntimeError, 'probe failure'):
            cache_recording_durations(self.db, self.source, '2026-09-03', probe=failing_probe)
        p = self._probe()
        result = cache_recording_durations(self.db, self.source, '2026-09-03', probe=p)
        self.assertEqual((result.probed, result.cache_hits), (1, 1))

    def test_rejects_changed_source_during_probe(self):
        def changing_probe(path):
            with path.open('ab') as f:
                f.write(b'x')
            return RecordingDuration(path, 600_000)
        with self.assertRaisesRegex(RuntimeError, 'Source changed'):
            cache_recording_durations(self.db, self.source, '2026-09-03', probe=changing_probe)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            durations = [r[0] for r in conn.execute('SELECT duration_ms FROM recordings')]
        self.assertEqual(durations, [None, None])

    def test_source_files_untouched_by_successful_cache(self):
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in self.paths]
        cache_recording_durations(self.db, self.source, '2026-09-03', probe=self._probe())
        after = [(p.read_bytes(), p.stat().st_mtime_ns) for p in self.paths]
        self.assertEqual(before, after)

    def test_no_matching_date_does_not_launch_probe(self):
        probe = self._probe()
        with self.assertRaisesRegex(ValueError, 'No indexed recordings'):
            cache_recording_durations(self.db, self.source, '2026-09-04', probe=probe)
        probe.assert_not_called()

    def test_progress_reports_processed_and_hits(self):
        updates = []
        cache_recording_durations(self.db, self.source, '2026-09-03',
                                  probe=self._probe(), on_progress=lambda *args: updates.append(args))
        self.assertEqual(updates, [(1, 2, 0, 1), (2, 2, 0, 2)])


if __name__ == '__main__':
    unittest.main()
