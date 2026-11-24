"""Read Matroska duration metadata without decoding or modifying media.

MKVToolNix `mkvmerge -J` reports container.properties.duration
in nanoseconds. No source content is changed.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .merger import DEFAULT_MKVMERGE


class DurationProbeError(RuntimeError):
    """Duration metadata is unavailable or invalid."""


@dataclass(frozen=True)
class RecordingDuration:
    path: Path
    duration_ms: int
    tool: str = "mkvmerge"


def duration_ms_from_identification(payload: object) -> int:
    """Parse MKVToolNix JSON; fail closed on missing/invalid duration."""
    if not isinstance(payload, dict):
        raise DurationProbeError("mkvmerge identification must be a JSON object")
    container = payload.get("container")
    if not isinstance(container, dict):
        raise DurationProbeError("container identification missing")
    properties = container.get("properties")
    if not isinstance(properties, dict):
        raise DurationProbeError("container properties missing")
    duration_ns = properties.get("duration")
    if isinstance(duration_ns, bool) or not isinstance(duration_ns, int) or duration_ns <= 0:
        raise DurationProbeError("positive integer duration (ns) missing")
    return max(1, (duration_ns + 500_000) // 1_000_000)


def probe_recording_duration(
    path: str | Path,
    mkvmerge: str | Path = DEFAULT_MKVMERGE,
    timeout_seconds: float = 30.0,
) -> RecordingDuration:
    """Identify an MKV using mkvmerge -J. Does not read/decode full media."""
    source = Path(path)
    executable = Path(mkvmerge)
    if not source.is_file():
        raise FileNotFoundError(f"MKV source missing: {source}")
    if source.suffix.lower() != ".mkv":
        raise ValueError(f"Not an MKV file: {source}")
    if not executable.is_file():
        raise FileNotFoundError(f"mkvmerge missing: {executable}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")

    try:
        result = subprocess.run(
            [str(executable), "-J", str(source)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DurationProbeError(f"Duration probe timed out: {source}") from exc
    except OSError as exc:
        raise DurationProbeError(f"Failed to start mkvmerge: {source}: {exc}") from exc

    if result.returncode != 0:
        raise DurationProbeError(
            f"mkvmerge identification failed (exit {result.returncode}): "
            f"{source}: {result.stderr[-500:]}"
        )
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise DurationProbeError(f"Invalid mkvmerge JSON: {source}") from exc
    return RecordingDuration(source, duration_ms_from_identification(payload))
