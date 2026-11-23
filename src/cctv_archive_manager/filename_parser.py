
"""Agent DVR MKV 파일명 분석 모듈."""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


# 예: 2026-09-02_00-06-03_454.mkv
FILENAME_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_"
    r"(?P<hour>\d{2})-"
    r"(?P<minute>\d{2})-"
    r"(?P<second>\d{2})_"
    r"(?P<millisecond>\d{3})\.mkv$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RecordingInfo:
    """파일명에서 분석한 녹화 정보."""

    filename: str
    recorded_at: datetime

    @property
    def date(self) -> str:
        return self.recorded_at.strftime("%Y-%m-%d")

    @property
    def time(self) -> str:
        return self.recorded_at.strftime("%H:%M:%S")

    @property
    def month(self) -> str:
        return self.recorded_at.strftime("%Y-%m")


def parse_recording_filename(
    filename: str,
) -> RecordingInfo | None:
    """Agent DVR 파일명을 분석한다.

    정상적인 파일명은 RecordingInfo로 반환한다.
    규칙이 다르거나 잘못된 날짜이면 None을 반환한다.
    """

    name = Path(filename).name
    match = FILENAME_PATTERN.fullmatch(name)

    if match is None:
        return None

    values = match.groupdict()

    try:
        timestamp = datetime.strptime(
            (
                f"{values['date']} "
                f"{values['hour']}:"
                f"{values['minute']}:"
                f"{values['second']}."
                f"{values['millisecond']}"
            ),
            "%Y-%m-%d %H:%M:%S.%f",
        )
    except ValueError:
        return None

    return RecordingInfo(
        filename=name,
        recorded_at=timestamp,
    )
