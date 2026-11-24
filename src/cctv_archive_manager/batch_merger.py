"""Execute prevalidated MKV merge groups sequentially, with cancellation support."""

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .merge_planner import MergeGroup
from .merger import DEFAULT_MKVMERGE, MergeResult, prepare_merge
from .progress_merger import MergeCancelled, merge_with_progress


@dataclass(frozen=True)
class BatchMergeResult:
    completed: tuple[MergeResult, ...]
    total_groups: int


class GroupMergeError(RuntimeError):
    """A group failed; previous successful outputs are preserved."""

    def __init__(
        self,
        failed_group: int,
        total_groups: int,
        completed: tuple[MergeResult, ...],
        reason: Exception,
    ) -> None:
        self.failed_group = failed_group
        self.total_groups = total_groups
        self.completed = completed
        super().__init__(
            f"그룹 {failed_group}/{total_groups} 병합 실패: {reason}\n"
            f"이미 완료된 그룹: {len(completed)}개"
        )


class BatchMergeCancelled(MergeCancelled):
    """Batch cancellation with successfully completed results attached."""

    def __init__(
        self,
        cancelled_group: int,
        total_groups: int,
        completed: tuple[MergeResult, ...],
    ) -> None:
        self.cancelled_group = cancelled_group
        self.total_groups = total_groups
        self.completed = completed
        super().__init__(
            f"그룹 {cancelled_group}/{total_groups}에서 병합이 취소됐습니다. "
            f"완료된 그룹 {len(completed)}개의 출력 파일은 보존합니다."
        )


def merge_groups(
    groups: Sequence[MergeGroup],
    output_dir: str | Path,
    on_progress: Callable[[int, int, int], None] | None = None,
    mkvmerge: str | Path = DEFAULT_MKVMERGE,
    cancel_event: threading.Event | None = None,
) -> BatchMergeResult:
    """Validate the entire batch, then merge one group at a time.

    ``cancel_event`` is optional, preserving older callers. Cancellation
    never deletes already completed outputs. A partially written MKV is
    handled by ``merge_with_progress`` and is not silently removed here.

    Raise ``BatchMergeCancelled`` for cancellation and ``GroupMergeError``
    for other group failures. Preflight failures retain their original type.
    """
    groups = tuple(groups)
    output_dir = Path(output_dir)

    if not groups:
        raise ValueError("병합할 그룹이 없습니다.")

    if not output_dir.is_dir():
        raise NotADirectoryError(f"출력 폴더가 없습니다: {output_dir}")

    total_groups = len(groups)
    jobs: list[tuple[MergeGroup, Path]] = []
    seen_outputs: set[str] = set()
    seen_inputs: set[str] = set()

    # Validate ALL groups before creating any MKV or lock file.
    for group in groups:
        name = group.output_name
        if not name or Path(name).name != name or name in (".", ".."):
            raise ValueError(f"유효하지 않은 출력 파일명: {name!r}")

        destination = output_dir / name
        output_key = str(destination.resolve()).casefold()
        if output_key in seen_outputs:
            raise ValueError(f"중복 출력 파일: {destination}")
        seen_outputs.add(output_key)

        for file in group.files:
            input_key = str(Path(file).resolve()).casefold()
            if input_key in seen_inputs:
                raise ValueError(f"여러 그룹에 중복된 원본 파일: {file}")
            seen_inputs.add(input_key)

        prepare_merge(group.files, destination, mkvmerge=mkvmerge)

        partial = destination.with_name(destination.stem + ".partial.mkv")
        lock = destination.with_name(destination.name + ".lock")
        if partial.exists() or lock.exists():
            raise FileExistsError(
                f"기존 임시 파일 또는 잠금 파일이 있습니다: {destination}"
            )
        jobs.append((group, destination))

    completed: list[MergeResult] = []
    for index, (group, destination) in enumerate(jobs, start=1):
        # Important even if the previous merge finished just as cancellation
        # was requested: do not launch the next group.
        if cancel_event is not None and cancel_event.is_set():
            raise BatchMergeCancelled(index, total_groups, tuple(completed))

        def report_progress(percent: int, group_index: int = index) -> None:
            if on_progress is not None:
                on_progress(group_index, total_groups, percent)

        kwargs = {
            "inputs": group.files,
            "output": destination,
            "on_progress": report_progress,
            "mkvmerge": mkvmerge,
        }
        if cancel_event is not None:
            kwargs["cancel_event"] = cancel_event

        try:
            result = merge_with_progress(**kwargs)
        except MergeCancelled as error:
            raise BatchMergeCancelled(index, total_groups, tuple(completed)) from error
        except Exception as error:
            raise GroupMergeError(index, total_groups, tuple(completed), error) from error

        completed.append(result)

    return BatchMergeResult(tuple(completed), total_groups)
