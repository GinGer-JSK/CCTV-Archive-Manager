"""Plan contiguous MKV groups balanced by actual playback duration.

No filesystem, network, video data, or SQLite mutations occur here.
Accepts RecordingDuration-like objects with `path` and `duration_ms`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, Sequence

_FILENAME = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})_(\d{3})\.mkv$", re.IGNORECASE)


class HasRecordingDuration(Protocol):
    path: Path
    duration_ms: int


@dataclass(frozen=True)
class DurationMergeGroup:
    date: str
    part_number: int
    files: tuple[Path, ...]
    output_name: str
    total_duration_ms: int


def plan_duration_groups(
    recordings: Sequence[HasRecordingDuration],
    parts: int,
    *,
    min_files_per_group: int = 2,
) -> list[DurationMergeGroup]:
    """Split one day's MKVs into *contiguous* time-balanced groups.

    Minimize worst absolute deviation from the ideal duration; use the
    sum of squared deviations as a deterministic tie-breaker. Entire
    MKVs only: no frame splitting and no re-encoding.
    """
    if isinstance(parts, bool) or not isinstance(parts, int) or parts < 1:
        raise ValueError("parts must be a positive integer")
    if (isinstance(min_files_per_group, bool) or
            not isinstance(min_files_per_group, int) or min_files_per_group < 1):
        raise ValueError("min_files_per_group must be a positive integer")
    if len(recordings) < parts * min_files_per_group:
        raise ValueError("not enough recordings for requested groups")

    entries: list[tuple[datetime, Path, int, str]] = []
    seen: set[str] = set()
    day: str | None = None
    for recording in recordings:
        path = Path(recording.path)
        match = _FILENAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"invalid Agent DVR MKV filename: {path.name}")
        date, h, m, s, millis = match.groups()
        try:
            timestamp = datetime.strptime(f"{date} {h}:{m}:{s}.{millis}", "%Y-%m-%d %H:%M:%S.%f")
        except ValueError as exc:
            raise ValueError(f"invalid recording timestamp: {path.name}") from exc
        if day is not None and day != date:
            raise ValueError("recordings from different dates cannot be mixed")
        day = date
        key = str(path).casefold()
        if key in seen:
            raise ValueError(f"duplicate recording path: {path}")
        seen.add(key)
        duration = recording.duration_ms
        if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
            raise ValueError(f"duration_ms must be a positive integer: {path}")
        entries.append((timestamp, path, duration, date))

    # Filename timestamps are used only for chronological order.
    entries.sort(key=lambda x: (x[0], str(x[1]).casefold()))
    n = len(entries)
    prefix = [0]
    for entry in entries:
        prefix.append(prefix[-1] + entry[2])
    total = prefix[-1]

    # dp[g][end] = (largest scaled deviation, sum of squared scaled
    # deviations, predecessor index).  All arithmetic is exact integer.
    dp: list[dict[int, tuple[int, int, int]]] = [{0: (0, 0, -1)}]
    for group_index in range(1, parts + 1):
        layer: dict[int, tuple[int, int, int]] = {}
        min_end = group_index * min_files_per_group
        max_end = n - (parts - group_index) * min_files_per_group
        for end in range(min_end, max_end + 1):
            best: tuple[int, int, int] | None = None
            for start in range((group_index - 1) * min_files_per_group,
                               end - min_files_per_group + 1):
                previous = dp[-1].get(start)
                if previous is None:
                    continue
                duration = prefix[end] - prefix[start]
                deviation = abs(duration * parts - total)
                candidate = (
                    max(previous[0], deviation),
                    previous[1] + deviation * deviation,
                    -start,  # Favor fuller earlier groups for equal scores.
                )
                if best is None or candidate < best:
                    best = candidate
            if best is not None:
                layer[end] = best
        dp.append(layer)

    if n not in dp[parts]:
        raise ValueError("unable to construct duration-balanced groups")

    boundaries = [n]
    end = n
    for group_index in range(parts, 0, -1):
        start = -dp[group_index][end][2]
        boundaries.append(start)
        end = start
    boundaries.reverse()

    result: list[DurationMergeGroup] = []
    assert day is not None
    for part_number, (start, end) in enumerate(zip(boundaries[:-1], boundaries[1:]), 1):
        result.append(DurationMergeGroup(
            date=day,
            part_number=part_number,
            files=tuple(item[1] for item in entries[start:end]),
            output_name=f"{day}_part{part_number:02d}.mkv",
            total_duration_ms=prefix[end] - prefix[start],
        ))
    return result
