
"""CCTV MKV 날짜별 병합 그룹 계획."""

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .filename_parser import parse_recording_filename


@dataclass(frozen=True)
class MergeGroup:
    date: str
    part_number: int
    files: tuple[Path, ...]

    @property
    def output_name(self) -> str:
        return (
            f"{self.date}_part"
            f"{self.part_number:02d}.mkv"
        )


def plan_merge_groups(
    files: Sequence[str | Path],
    parts: int,
) -> list[MergeGroup]:
    """같은 날짜의 MKV를 시간순으로 균등 분할한다.

    파일을 읽거나 수정하지 않고 경로와 파일명만 분석한다.
    각 그룹은 최소 2개 파일을 포함해야 한다.
    """

    paths = [Path(item) for item in files]

    if isinstance(parts, bool) or not isinstance(parts, int):
        raise ValueError("출력 파일 개수는 정수여야 합니다.")

    if parts < 1:
        raise ValueError("출력 파일 개수는 1 이상이어야 합니다.")

    if len(paths) < 2:
        raise ValueError("병합에는 최소 2개 MKV가 필요합니다.")

    if len(set(paths)) != len(paths):
        raise ValueError("중복된 파일 경로가 존재합니다.")

    recordings = []

    for path in paths:
        info = parse_recording_filename(path.name)

        if info is None:
            raise ValueError(
                f"파일명 형식이 올바르지 않습니다: {path.name}"
            )

        recordings.append((info.recorded_at, info.date, path))

    dates = {item[1] for item in recordings}

    if len(dates) != 1:
        raise ValueError(
            "서로 다른 녹화 날짜를 하나의 병합 계획으로 "
            "묶을 수 없습니다."
        )

    if parts * 2 > len(paths):
        raise ValueError(
            "각 병합 그룹에는 최소 2개 MKV가 필요합니다."
        )

    recordings.sort(key=lambda item: item[0])

    # 같은 녹화 시각은 파일 경로를 기준으로 추가 정렬
    recordings.sort(key=lambda item: (item[0], str(item[2])))

    date = recordings[0][1]

    base_size, remainder = divmod(len(recordings), parts)

    groups = []
    offset = 0

    for index in range(parts):
        group_size = base_size + (1 if index < remainder else 0)

        selected = recordings[
            offset:offset + group_size
        ]

        groups.append(
            MergeGroup(
                date=date,
                part_number=index + 1,
                files=tuple(item[2] for item in selected),
            )
        )

        offset += group_size

    return groups
