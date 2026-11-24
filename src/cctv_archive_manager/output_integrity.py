"""Read-only verification of completed MKV outputs.

'metadata' confirms MKV structure with mkvmerge -J and computes a SHA-256
fingerprint of file bytes; it does NOT guarantee all frames decode.
'decode' additionally requests FFmpeg to decode all video/audio streams.
No tool invocation uses the input path as an output destination.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class OutputIntegrityError(RuntimeError):
    """MKV output could not be validated; do not checkpoint or skip it."""


@dataclass(frozen=True)
class OutputIntegrityReport:
    path: Path
    bytes: int
    sha256: str
    tracks: int
    duration_ns: int
    decoded: bool


def _executable(name: str, explicit: str | Path | None, env_var: str) -> str:
    candidate = explicit if explicit is not None else os.environ.get(env_var)
    if candidate:
        value = str(candidate)
        if Path(value).is_file():
            return value
        resolved = shutil.which(value)
        if resolved:
            return resolved
        raise OutputIntegrityError(f'{name} executable not found: {value}')
    found = shutil.which(name)
    if not found and name == 'mkvmerge':
        windows_default = Path('C:/Program Files/MKVToolNix/mkvmerge.exe')
        if windows_default.is_file():
            found = str(windows_default)
    if not found:
        raise OutputIntegrityError(f'{name} executable not found; set {env_var}')
    return found


def _run(args: list[str], timeout_seconds: float, label: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=timeout_seconds, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OutputIntegrityError(f'{label} failed: {exc}') from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout)[-700:]
        raise OutputIntegrityError(f'{label} exit={result.returncode}: {message}')
    return result


def inspect_mkv_output(
    path: str | Path, *,
    mkvmerge: str | Path | None = None,
    ffmpeg: str | Path | None = None,
    decode: bool = False,
    identify_timeout_seconds: float = 30,
    decode_timeout_seconds: float = 3600,
    chunk_bytes: int = 1024 * 1024,
) -> OutputIntegrityReport:
    """Validate a local MKV without changing it; fail closed on uncertainty.

    Re-stat around expensive operations to detect concurrent mutation.
    The SHA-256 represents bytes observed during this invocation; it is
    verified against the saved manifest on a later resume.
    """
    target = Path(path)
    if target.suffix.lower() != '.mkv' or not target.is_file():
        raise OutputIntegrityError(f'output is not an existing MKV: {target}')
    if identify_timeout_seconds <= 0 or decode_timeout_seconds <= 0 or chunk_bytes <= 0:
        raise ValueError('timeouts and chunk_bytes must be positive')
    before = target.stat()
    if before.st_size <= 0:
        raise OutputIntegrityError(f'empty MKV output: {target}')

    identifier = _executable('mkvmerge', mkvmerge, 'MKVMERGE_EXE')
    result = _run([identifier, '-J', str(target)], identify_timeout_seconds, 'mkvmerge -J')
    try:
        info = json.loads(result.stdout)
        tracks = info['tracks']
        props = info['container']['properties']
        duration = props['duration']
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise OutputIntegrityError('invalid mkvmerge identification JSON') from exc
    if (not isinstance(tracks, list) or not tracks or
            not any(isinstance(t, dict) and t.get('type') in ('video', 'audio') for t in tracks) or
            isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0):
        raise OutputIntegrityError('no playable track or positive duration in MKV')

    if decode:
        decoder = _executable('ffmpeg', ffmpeg, 'FFMPEG_EXE')
        # Optional ? maps avoid requiring both audio and video; subtitles not mapped.
        _run([decoder, '-nostdin', '-hide_banner', '-loglevel', 'error', '-xerror',
              '-i', str(target), '-map', '0:v?', '-map', '0:a?', '-f', 'null', '-'],
             decode_timeout_seconds, 'ffmpeg full decode')

    digest = hashlib.sha256()
    try:
        with target.open('rb') as reader:
            while data := reader.read(chunk_bytes):
                digest.update(data)
    except OSError as exc:
        raise OutputIntegrityError(f'cannot hash MKV: {target}: {exc}') from exc
    after = target.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise OutputIntegrityError(f'MKV changed while inspecting: {target}')
    return OutputIntegrityReport(target, after.st_size, digest.hexdigest(), len(tracks), duration, decode)
