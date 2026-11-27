"""Multi-format filename and source isolation integration tests."""
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from cctv_archive_manager.filename_parser import (
    analyze_recording_filename, parse_recording_filename, FILENAME_PATTERN,
)
from cctv_archive_manager.source_guard import validate_single_source
from cctv_archive_manager.catalog import index_recordings
from cctv_archive_manager.catalog_reader import get_cached_files
from cctv_archive_manager.incremental_sync import sync_new_recordings
from cctv_archive_manager.file_scanner import scan_recordings
from cctv_archive_manager.duration_cache import cache_recording_durations
from cctv_archive_manager.duration_probe import RecordingDuration
from cctv_archive_manager.merge_planner import plan_merge_groups
from cctv_archive_manager.duration_planner import plan_duration_groups
from cctv_archive_manager.merger import prepare_merge

STAMPS = (
    '2026-11-27_13-30-00_123', '2026-11-27_13-30-00',
    '20261127-133000', '20261127_133000', '20261127133000',
    '2026_11_27_13_30_00_123', '2026_11_27_13_30_00',
    '2026-11-27_133000', '20261127_133000_123',
    '2026-11-27_13-30-00-123', '20261127-133000-123',
    '20261127133000123',
)


class MultiFormatTests(unittest.TestCase):
    def test_all_patterns_and_labels(self):
        for i, stamp in enumerate(STAMPS, 1):
            for pre, suf in (('', ''), ('현관 Camera_', ''), ('', '_[CH1]'),
                             ('FrontDoor_', '_CH1'), ('4_', '')):
                name = pre + stamp + suf + '.MKV'
                with self.subTest(name=name):
                    m = analyze_recording_filename(name)
                    self.assertIsNotNone(m)
                    self.assertEqual(m.pattern_id, f'P{i:02d}')
                    self.assertEqual(m.prefix, pre[:-1] or None)
                    self.assertEqual(m.suffix, suf[1:] or None)
                    self.assertEqual(m.recording.recorded_at, datetime(
                        2026, 11, 27, 13, 30, 0,
                        123000 if i in (1, 6, 9, 10, 11, 12) else 0))
                    self.assertEqual(parse_recording_filename(name), m.recording)

    def test_invalid_and_ambiguous(self):
        for stem in ('20260230-133000', '20261127-253000',
                     '2026_02_30_13_30_00_123', '20261127133099',
                     '20261127-133000_20261128-133000',
                     '20261127-133000_20261127-133000',
                     '20260230-133000_20261127-133000',
                     '2026-11-27_13-30-00_123_',
                     '2026-11-27_13-30-00_12', '2026-11-27_13-30-00_1234',
                     '20261127-133000_42',
                     '20261127-133000_part01', '20261127-133000.partial',
                     '20261127-133000_CH1.partial', '2026-11-27_part01',
                     '12320261127133000', '202611271330001234', 'unknown'):
            with self.subTest(stem=stem):
                self.assertIsNone(parse_recording_filename(stem + '.mkv'))

    def test_leap_day_and_extension(self):
        self.assertIsNotNone(parse_recording_filename('20240229-235959.MKV'))
        self.assertIsNone(parse_recording_filename('20230229-235959.mkv'))
        self.assertIsNone(parse_recording_filename('20240229-235959.mp4'))

    def test_guard(self):
        validate_single_source([Path('Camera_20261127-133000.mkv'),
                                Path('20261127-134000_camera.mkv')])
        validate_single_source([Path('4_20261127-133000.mkv'),
                                Path('20261127-134000.mkv')])
        validate_single_source([Path('FrontDoor_20261127-133000_CH1.mkv'),
                                Path('FrontDoor_20261127-134000_CH1.mkv')])
        for names in (('20261127-133000_CH1.mkv', '20261127-134000_CH2.mkv'),
                      ('CH1_20261127-133000_CH2.mkv',),
                      ('A_20261127-133000.mkv', '20261127-134000_B.mkv'),
                      ('a/20261127-133000.mkv', 'b/20261127-134000.mkv'),
                      ('unknown.mkv',)):
            with self.subTest(names=names), self.assertRaises(ValueError):
                validate_single_source([Path(n) for n in names])

    def test_catalog_upgrade_and_merge_preparation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / 'source'
            source.mkdir()
            db = root / 'catalog.sqlite3'
            names = ('Camera_2026-11-27_13-00-00_123.mkv',
                     '20261127-131000_Camera.mkv',
                     'Camera_2026_11_27_13_20_00_123.mkv')
            paths = [source / n for n in names]
            for p in paths:
                p.write_bytes(b'test-only')
                os.utime(p, (1000000000, 1000000000))
            # Simulate the pre-upgrade catalog with only the old format.
            def legacy_only(name):
                return parse_recording_filename(name) if FILENAME_PATTERN.fullmatch(name) else None
            with patch('cctv_archive_manager.catalog.parse_recording_filename', legacy_only):
                self.assertEqual(index_recordings(source, db), 1)
            def probe(p):
                return RecordingDuration(p, 1000)
            cache_recording_durations(db, source, '2026-11-27', probe=probe)
            result = sync_new_recordings(source, db, minimum_age_seconds=900)
            self.assertEqual((result.already_indexed, result.added), (1, 2))
            self.assertEqual(sync_new_recordings(source, db).added, 0)
            self.assertEqual(scan_recordings(source).valid_mkv, 3)
            self.assertEqual(get_cached_files(db, source, '2026-11-27'), paths)
            cached = cache_recording_durations(db, source, '2026-11-27', probe=probe)
            self.assertEqual((cached.cache_hits, cached.probed), (1, 2))
            self.assertEqual(plan_merge_groups(paths[::-1], 1)[0].files, tuple(paths))
            self.assertEqual(plan_duration_groups(cached.records[::-1], 1)[0].files, tuple(paths))
            exe = root / 'fake.exe'
            exe.touch()
            command, _ = prepare_merge(paths[::-1], root / 'out.mkv', exe)
            self.assertEqual(command[3:], [str(paths[0]), '+', str(paths[1]), '+', str(paths[2])])
            self.assertTrue(all(p.read_bytes() == b'test-only' for p in paths))
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
