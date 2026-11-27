
"""Multi-format MKV filename parser."""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


# 예: 2026-09-02_00-06-03_454.mkv
FILENAME_PATTERN = re.compile(
    r"^(?:(?P<prefix>[^/\\\r\n]+)_)?(?P<date>\d{4}-\d{2}-\d{2})_"
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


# Underscores delimit optional labels; timestamp separators are explicit.
# Keep the legacy regex exported for compatibility, not for source checks.
@dataclass(frozen=True)
class FilenameMatch:
    recording: RecordingInfo
    pattern_id: str
    prefix: str | None
    suffix: str | None


_Y = r"(?P<year>[0-9]{4})"
_M = r"(?P<month>[0-9]{2})"
_D = r"(?P<day>[0-9]{2})"
_H = r"(?P<hour>[0-9]{2})"
_m = r"(?P<minute>[0-9]{2})"
_S = r"(?P<second>[0-9]{2})"
_MS = r"(?P<millisecond>[0-9]{3})"


def _pattern(date_sep, join, time_sep, millis_sep=None):
    body = date_sep.join((_Y, _M, _D)) + join + time_sep.join((_H, _m, _S))
    if millis_sep is not None:
        body += millis_sep + _MS
    # Lookahead permits overlapping candidates, so ambiguity is not hidden.
    return re.compile(r"(?=(?P<stamp>" + body + r")(?=_|$))")


PATTERN_REGISTRY = (
    ("P01", _pattern("-", "_", "-", "_")),
    ("P02", _pattern("-", "_", "-")),
    ("P03", _pattern("", "-", "")),
    ("P04", _pattern("", "_", "")),
    ("P05", _pattern("", "", "")),
    ("P06", _pattern("_", "_", "_", "_")),
    ("P07", _pattern("_", "_", "_")),
    ("P08", _pattern("-", "_", "")),
    ("P09", _pattern("", "_", "", "_")),
    ("P10", _pattern("-", "_", "-", "-")),
    ("P11", _pattern("", "-", "", "-")),
    ("P12", _pattern("", "", "", "")),
)


def analyze_recording_filename(filename: str) -> FilenameMatch | None:
    """Return a unique structural match, or None on invalid/ambiguous names."""
    name = Path(filename).name
    if not name.lower().endswith(".mkv") or any(c in name for c in "/\\\r\n"):
        return None
    stem = name[:-4]
    if stem.endswith("_") or stem.lower().endswith((".partial", ".mkv")):
        return None
    candidates = []
    for pattern_id, pattern in PATTERN_REGISTRY:
        for match in pattern.finditer(stem):
            start, end = match.span("stamp")
            if start and stem[start - 1] != "_":
                continue
            # Empty labels (a single delimiter) are not meaningful.
            if start == 1:
                continue
            candidates.append((start, end, pattern_id, match))
    # A longer timestamp starting at the same position owns milliseconds.
    # Do this BEFORE validation: invalid long forms must not fall back.
    maximal = [c for c in candidates if not any(
        d[0] == c[0] and d[1] > c[1] for d in candidates
    )]
    if len(maximal) != 1:
        return None
    start, end, pattern_id, match = maximal[0]
    g = match.groupdict()
    try:
        timestamp = datetime(*(int(g[k]) for k in
            ("year", "month", "day", "hour", "minute", "second")),
            int(g.get("millisecond") or 0) * 1000)
    except ValueError:
        return None
    prefix = stem[:start - 1] if start else None
    suffix = stem[end + 1:] if end < len(stem) else None
    # A numeric-only suffix may be a malformed fractional timestamp.
    if suffix and suffix.isascii() and suffix.isdigit():
        return None
    # Do not accidentally accept generated merge artifacts as recordings.
    if suffix and re.fullmatch(r"part[0-9]+(?:\.partial)?", suffix, re.IGNORECASE):
        return None
    return FilenameMatch(RecordingInfo(name, timestamp), pattern_id, prefix, suffix)


def parse_recording_filename(filename: str) -> RecordingInfo | None:
    """Backward-compatible public interface used by catalog and planners."""
    match = analyze_recording_filename(filename)
    return match.recording if match else None
