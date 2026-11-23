
"""MKVToolNix 기반의 MKV 병합 엔진.

원본 파일은 읽기만 하며, 별도 출력 경로에 결과물을 만든다.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .filename_parser import parse_recording_filename


DEFAULT_MKVMERGE = Path(
    r"C:\Program Files\MKVToolNix\mkvmerge.exe"
)


@dataclass(frozen=True)
class MergeResult:
    output: Path
    input_count: int
    output_bytes: int
    return_code: int
    log: str


def prepare_merge(
    inputs: Sequence[str | Path],
    output: str | Path,
    mkvmerge: str | Path = DEFAULT_MKVMERGE,
) -> tuple[list[str], Path]:
    """병합 명령을 작성하고 입력·출력 경로를 검증한다."""

    executable = Path(mkvmerge)
    source_files = [Path(item) for item in inputs]
    destination = Path(output)

    if not executable.is_file():
        raise FileNotFoundError(
            f"mkvmerge 실행 파일을 찾을 수 없습니다: {executable}"
        )

    if len(source_files) < 2:
        raise ValueError("병합에는 최소 2개 파일이 필요합니다.")

    if destination.suffix.lower() != ".mkv":
        raise ValueError("출력 파일의 확장자는 .mkv여야 합니다.")

    if destination.exists():
        raise FileExistsError(
            f"이미 출력 파일이 존재합니다: {destination}"
        )

    if not destination.parent.is_dir():
        raise NotADirectoryError(
            f"출력 폴더가 없습니다: {destination.parent}"
        )

    resolved_sources = [p.resolve() for p in source_files]
    resolved_output = destination.resolve()

    if len(set(resolved_sources)) != len(resolved_sources):
        raise ValueError("중복된 입력 파일이 있습니다.")

    dates = set()

    for source in source_files:
        if not source.is_file():
            raise FileNotFoundError(
                f"입력 파일을 찾을 수 없습니다: {source}"
            )

        info = parse_recording_filename(source.name)

        if info is None:
            raise ValueError(
                f"파일명 규칙이 일치하지 않습니다: {source.name}"
            )

        dates.add(info.date)

        # 출력물을 원본 녹화 폴더 안에 저장하지 않음
        if resolved_output.is_relative_to(source.resolve().parent):
            raise ValueError(
                "출력 파일은 원본 폴더 밖에 저장해야 합니다."
            )

    if len(dates) != 1:
        raise ValueError(
            "이번 테스트에서는 같은 날짜의 파일만 병합합니다."
        )

    # 파일명에 포함된 녹화 시작 시각을 기준으로 정렬
    ordered = sorted(
        source_files,
        key=lambda p: parse_recording_filename(
            p.name
        ).recorded_at,
    )

    command = [
        str(executable),
        "--output",
        str(destination),
        str(ordered[0]),
    ]

    for source in ordered[1:]:
        command.extend(["+", str(source)])

    return command, destination


def merge_recordings(
    inputs: Sequence[str | Path],
    output: str | Path,
    mkvmerge: str | Path = DEFAULT_MKVMERGE,
) -> MergeResult:
    """MKV 파일들을 시간순으로 연결한다."""

    command, destination = prepare_merge(
        inputs,
        output,
        mkvmerge,
    )

    # 생성 도중에도 같은 출력 경로를 다른 작업이 사용하지 못하게
    # 임시 파일을 먼저 독점 생성한다.
    temporary = destination.with_name(
        destination.stem + ".partial.mkv"
    )

    if temporary.exists():
        raise FileExistsError(
            f"임시 병합 파일이 이미 존재합니다: {temporary}"
        )

    # mkvmerge가 실제 기록할 경로만 임시 파일로 변경
    command[2] = str(temporary)

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

        log = result.stdout + "\n" + result.stderr

        if result.returncode not in (0, 1):
            raise RuntimeError(
                f"mkvmerge 실행 실패 (코드 {result.returncode})\n"
                + log[-3000:]
            )

        if not temporary.is_file():
            raise RuntimeError(
                "mkvmerge 출력 파일이 생성되지 않았습니다."
            )

        size = temporary.stat().st_size

        if size == 0:
            raise RuntimeError(
                "병합 결과 파일의 크기가 0바이트입니다."
            )

        # 코드 1은 경고를 동반한 완료를 뜻하므로
        # 자동 확정하지 않고 검증 대상으로 남긴다.
        if result.returncode == 1:
            raise RuntimeError(
                "mkvmerge 경고 발생. 임시 파일을 보존합니다.\n"
                + log[-3000:]
            )

        # 임시 파일에서 최종 파일로 확정
        temporary.rename(destination)

        return MergeResult(
            output=destination,
            input_count=len(inputs),
            output_bytes=size,
            return_code=result.returncode,
            log=log,
        )

    except Exception:
        # 실패한 .partial.mkv 파일은 조사할 수 있도록 보존한다.
        # 정상 원본은 삭제하거나 변경하지 않는다.
        raise
