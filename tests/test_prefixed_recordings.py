import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from cctv_archive_manager.filename_parser import parse_recording_filename
from cctv_archive_manager.file_scanner import scan_recordings
from cctv_archive_manager.catalog import index_recordings
from cctv_archive_manager.catalog_reader import get_cached_files
from cctv_archive_manager.merge_planner import plan_merge_groups
from cctv_archive_manager.duration_planner import plan_duration_groups
from cctv_archive_manager.merger import prepare_merge

TS = '2026-11-25_19-43-05_859.mkv'


class PrefixedRecordingTests(unittest.TestCase):
    def test_supported_prefixes(self):
        for prefix in ('', '4_', '5_', '7_', '123_', 'C500_', 'C545D-Wide_',
                       'CH1_C545D-Wide_', '[CH1]C545D-Wide_', '[CH2]C545D-Zoom_',
                       '[CH3]C120-01_', '[CH4]C120-02_', '[CH10]Camera-01_',
                       '[CH1] MediaMTX - C545DWide_', 'Front_Door_Camera_', '현관 카메라_'):
            with self.subTest(prefix=prefix):
                info = parse_recording_filename(prefix + TS)
                self.assertEqual(info.filename, prefix + TS)
                self.assertEqual(info.recorded_at, datetime(2026, 11, 25, 19, 43, 5, 859000))

    def test_invalid_names(self):
        for name in ('unknown.mkv', 'recording.mkv', '2026-13-25_19-43-05_859.mkv',
                     '2026-02-30_19-43-05_859.mkv', '2026-11-25_25-43-05_859.mkv',
                     '2026-11-25_19-70-05_859.mkv', '2026-11-25_19-43-99_859.mkv',
                     '[CH1]C545D-Wide_invalid.mkv', '[CH1]C545D-Wide_' + TS + '.tmp',
                     '4_2025-02-29_00-00-00_000.mkv'):
            with self.subTest(name=name):
                self.assertIsNone(parse_recording_filename(name))

    def test_boundaries(self):
        for stamp in ('2024-02-29_00-00-00_000', '2026-12-31_23-59-59_999',
                      '2027-01-01_00-00-00_000', '2026-04-30_23-59-59_999',
                      '2026-05-01_00-00-00_000'):
            with self.subTest(stamp=stamp):
                self.assertIsNotNone(parse_recording_filename('4_' + stamp + '.MKV'))

    def test_mixed_names_and_equal_timestamp_preserved(self):
        paths = [Path(x + TS) for x in ('5_', '', '4_', 'C500_')]
        expected = tuple(sorted(paths, key=str))
        self.assertEqual(plan_merge_groups(paths, 1)[0].files, expected)
        self.assertEqual(plan_duration_groups([SimpleNamespace(path=p, duration_ms=900000) for p in paths], 1)[0].files, expected)

    def test_both_planners_reject_mixed_sources(self):
        for paths in ([Path('a') / TS, Path('b') / TS],
                      [Path('[CH1]A_' + TS), Path('[CH2]B_' + TS)],
                      [Path('CameraA_' + TS), Path('CameraB_' + TS)]):
            with self.subTest(paths=paths):
                with self.assertRaises(ValueError):
                    plan_merge_groups(paths, 1)
                with self.assertRaises(ValueError):
                    plan_duration_groups([SimpleNamespace(path=p, duration_ms=900000) for p in paths], 1)

    def test_scan_catalog_and_command_literal_paths(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / '[CH1] 현관 - Camera_01'
            source.mkdir()
            paths = [source / ('[CH1] 현관_' + TS), source / '[CH1] 현관_2026-11-25_19-58-05_859.mkv']
            for p in paths:
                p.write_bytes(b'x')
            self.assertEqual(scan_recordings(source).valid_mkv, 2)
            db = root / 'test.sqlite3'
            self.assertEqual(index_recordings(source, db), 2)
            self.assertEqual(get_cached_files(db, source, '2026-11-25'), paths)
            exe = root / 'fake mkvmerge.exe'
            exe.touch()
            command, _ = prepare_merge(list(reversed(paths)), root / 'output.mkv', exe)
            self.assertEqual(command[3:], [str(paths[0]), '+', str(paths[1])])
            self.assertEqual([p.read_bytes() for p in paths], [b'x', b'x'])

    def test_execution_rejects_mixed_channels(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'mixed'
            source.mkdir()
            paths = [source / (prefix + TS) for prefix in ('[CH1]A_', '[CH2]B_')]
            for p in paths:
                p.touch()
            exe = root / 'fake.exe'
            exe.touch()
            with self.assertRaises(ValueError):
                prepare_merge(paths, root / 'output.mkv', exe)
