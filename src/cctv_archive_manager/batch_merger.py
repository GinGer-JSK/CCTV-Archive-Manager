
"""여러 MKV 병합 그룹을 순차적으로 실행한다."""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .merge_planner import MergeGroup
from .merger import (
    DEFAULT_MKVMERGE,
    MergeResult,
    prepare_merge,
)
from .progress_merger import merge_with_progress


@dataclass(frozen=True)
class BatchMergeResult:
    completed: tuple[MergeResult, ...]
    total_groups: int


class GroupMergeError(RuntimeError):
    """일부 그룹 완료 후 후속 병합 실패."""

    def __init__(
        self,
        failed_group: int,
        total_groups: int,
        completed: tuple[MergeResult, ...],
        reason: Exception,
    ):
        self.failed_group = failed_group
        self.total_groups = total_groups
        self.completed = completed

        super().__init__(
            f"그룹 {failed_group}/{total_groups} 병합 실패: "
            f"{reason}\n"
            f"이미 완료된 그룹: {len(completed)}개"
        )


def merge_groups(
    groups: Sequence[MergeGroup],
    output_dir: str | Path,
    on_progress: Callable[[int, int, int], None] | None = None,
    mkvmerge: str | Path = DEFAULT_MKVMERGE,
) -> BatchMergeResult:
    """병합 그룹을 순서대로 실행한다.

    on_progress(group_index, total_groups, percent)
    group_index는 1부터 시작한다.
    """

    groups = tuple(groups)
    output_dir = Path(output_dir)

    if not groups:
        raise ValueError("병합 그룹이 없습니다.")

    if not output_dir.is_dir():
        raise NotADirectoryError(
            f"출력 폴더가 없습니다: {output_dir}"
        )

    total_groups = len(groups)
    jobs = []
    seen_outputs = set()
    seen_inputs = set()

    # 모든 그룹을 실행 전에 검증
    for group in groups:
        name = group.output_name

        if (
            not name
            or Path(name).name != name
            or name in (".", "..")
        ):
            raise ValueError(
                f"잘못된 출력 파일명: {name}"
            )

        destination = output_dir / name

        output_key = str(destination.resolve()).casefold()

        if output_key in seen_outputs:
            raise ValueError(
                f"중복 출력 파일명: {name}"
            )

        seen_outputs.add(output_key)

        # 서로 다른 그룹에 같은 영상이 중복되는지 확인
        for file in group.files:
            input_key = str(Path(file).resolve()).casefold()

            if input_key in seen_inputs:
                raise ValueError(
                    f"그룹 간 중복 입력 파일: {file}"
                )

            seen_inputs.add(input_key)

        # 기존 병합 엔진의 입력·출력 검사 재사용
        prepare_merge(
            group.files,
            destination,
            mkvmerge=mkvmerge,
        )

        partial = destination.with_name(
            destination.stem + ".partial.mkv"
        )
        lock = destination.with_name(
            destination.name + ".lock"
        )

        for path in (partial, lock):
            if path.exists():
                raise FileExistsError(
                    f"기존 병합 관련 파일이 있습니다: {path}"
                )

        jobs.append((group, destination))

    # 검증이 모두 끝난 후에만 실제 병합
    completed = []

    for index, (group, destination) in enumerate(
        jobs, start=1
    ):

        def report_progress(
            percent: int,
            group_index=index,
        ):
            if on_progress is not None:
                on_progress(
                    group_index,
                    total_groups,
                    percent,
                )

        try:
            result = merge_with_progress(
                inputs=group.files,
                output=destination,
                on_progress=report_progress,
                mkvmerge=mkvmerge,
            )

        except Exception as error:
            raise GroupMergeError(
                failed_group=index,
                total_groups=total_groups,
                completed=tuple(completed),
                reason=error,
            ) from error

        completed.append(result)

    return BatchMergeResult(
        completed=tuple(completed),
        total_groups=total_groups,
    )
