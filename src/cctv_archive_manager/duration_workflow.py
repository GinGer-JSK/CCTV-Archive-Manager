"""Connect cached-duration planning to the existing sequential merge engine.

Planning updates only the local SQLite duration cache; it never merges video.
Execution is a separate explicit call using the existing batch merger protections.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .duration_cache import DurationCacheResult, cache_recording_durations
from .duration_planner import DurationMergeGroup, plan_duration_groups


@dataclass(frozen=True)
class DurationBatchPlan:
    cache: DurationCacheResult
    groups: tuple[DurationMergeGroup, ...]

    @property
    def total_duration_ms(self) -> int:
        return sum(group.total_duration_ms for group in self.groups)


def prepare_duration_batch(
    database: str | Path,
    source: str | Path,
    date: str,
    parts: int,
    *,
    on_probe_progress: Callable[[int, int, int, int], None] | None = None,
) -> DurationBatchPlan:
    """Analyze/cache durations and build a read-only, contiguous merge plan.

    No MKV output is produced. Source MKV files are never modified.
    The local SQLite database may be updated with duration metadata.
    """
    cache = cache_recording_durations(
        database, source, date, on_progress=on_probe_progress
    )
    groups = tuple(plan_duration_groups(cache.records, parts))
    if sum(len(group.files) for group in groups) != len(cache.records):
        raise RuntimeError("Duration planning lost input recordings")
    if sum(group.total_duration_ms for group in groups) != cache.total_duration_ms:
        raise RuntimeError("Duration planning lost playback duration")
    return DurationBatchPlan(cache=cache, groups=groups)


def execute_duration_batch(
    plan: DurationBatchPlan,
    output_dir: str | Path,
    *,
    on_progress: Callable[[int, int, int], None] | None = None,
    cancel_event=None,
):
    """Explicitly run the plan with existing batch-merger safety checks.

    The batch engine handles output conflicts, partial files, lock files,
    cancellation, and keeping completed groups. Never called by planning.
    """
    if not plan.groups:
        raise ValueError("No groups to merge")
    # Lazy import: planning must not launch, even import, the merger.
    from .batch_merger import merge_groups

    return merge_groups(
        groups=list(plan.groups),
        output_dir=Path(output_dir),
        on_progress=on_progress,
        cancel_event=cancel_event,
    )
