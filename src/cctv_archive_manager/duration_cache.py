"""Cache MKV playback durations in an existing recordings SQLite catalog.

Only the *local* SQLite DB is written. Source MKV files are never modified.
Existing indexer/sync INSERTs remain compatible with nullable cache columns.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .duration_probe import RecordingDuration, probe_recording_duration
from .merger import DEFAULT_MKVMERGE


@dataclass(frozen=True)
class DurationCacheResult:
    date: str
    records: tuple[RecordingDuration, ...]
    cache_hits: int
    probed: int
    elapsed_seconds: float

    @property
    def total_duration_ms(self) -> int:
        return sum(record.duration_ms for record in self.records)


def ensure_duration_columns(database: str | Path) -> None:
    """Idempotent, additive migration for an existing local recordings DB."""
    db = Path(database)
    if not db.is_file():
        raise FileNotFoundError(f"SQLite catalog does not exist: {db}")
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            existing = {row[1] for row in conn.execute('PRAGMA table_info(recordings)')}
            if not {'path', 'source', 'record_date', 'size_bytes', 'modified_ns'} <= existing:
                raise ValueError("SQLite recordings table is missing required columns")
            for name in ('duration_ms', 'duration_size_bytes', 'duration_modified_ns'):
                if name not in existing:
                    conn.execute(f'ALTER TABLE recordings ADD COLUMN {name} INTEGER')


def cache_recording_durations(
    database: str | Path,
    source: str | Path,
    date: str,
    *,
    mkvmerge: str | Path = DEFAULT_MKVMERGE,
    probe: Callable[[Path], RecordingDuration] | None = None,
    on_progress: Callable[[int, int, int, int], None] | None = None,
) -> DurationCacheResult:
    """Return duration records for one indexed day; probe only cache misses.

    Each file is stat'ed to invalidate stale durations even if the catalog's
    existing incremental sync has intentionally not refreshed metadata.
    Changes during a probe are rejected. Successfully probed entries persist
    incrementally, so a later retry can reuse them.

    on_progress(processed, total, hits, probed) is optional.
    """
    started = time.perf_counter()
    ensure_duration_columns(database)
    db = Path(database)
    source_key = str(Path(source).resolve())
    if probe is None:
        def probe(path: Path) -> RecordingDuration:
            return probe_recording_duration(path, mkvmerge=mkvmerge)

    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute(
            """SELECT path, size_bytes, modified_ns,
                      duration_ms, duration_size_bytes, duration_modified_ns
               FROM recordings WHERE source = ? AND record_date = ?
               ORDER BY recorded_at, path""",
            (source_key, date),
        ).fetchall()
        if not rows:
            raise ValueError(f"No indexed recordings for {date} in {source_key}")
        records = []
        hits = 0
        probed = 0
        for path_text, size, modified, duration, cached_size, cached_modified in rows:
            path = Path(path_text)
            # The existing catalog may be stale, so check source metadata.
            stat_before = path.stat()
            actual = (stat_before.st_size, stat_before.st_mtime_ns)
            if (type(duration) is int and duration > 0 and
                cached_size == actual[0] and cached_modified == actual[1] and
                size == actual[0] and modified == actual[1]):
                value = duration
                hits += 1
            else:
                result = probe(path)
                if result.path != path or type(result.duration_ms) is not int or result.duration_ms <= 0:
                    raise ValueError(f"Invalid duration probe result: {path}")
                stat_after = path.stat()
                if (stat_after.st_size, stat_after.st_mtime_ns) != actual:
                    raise RuntimeError(f"Source changed during duration probe: {path}")
                value = result.duration_ms
                # Preserve existing recording row; synchronize metadata if it
                # had changed since the last incremental catalog indexing.
                with conn:
                    conn.execute(
                        """UPDATE recordings SET size_bytes=?, modified_ns=?,
                           duration_ms=?, duration_size_bytes=?, duration_modified_ns=?
                           WHERE path=? AND source=? AND record_date=?""",
                        (actual[0], actual[1], value, actual[0], actual[1],
                         path_text, source_key, date),
                    )
                probed += 1
            records.append(RecordingDuration(path=path, duration_ms=value))
            if on_progress:
                on_progress(len(records), len(rows), hits, probed)
    return DurationCacheResult(date, tuple(records), hits, probed,
                               time.perf_counter() - started)
