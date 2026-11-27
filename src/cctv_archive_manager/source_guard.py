"""Conservative one-folder policy; filename labels are hints, not device identities."""
import re
from pathlib import Path
from typing import Sequence

from .filename_parser import analyze_recording_filename

_CHANNEL = re.compile(r"^(?:\[CH([0-9]+)\]|CH([0-9]+)(?=[ _-]|$))", re.IGNORECASE)


def validate_single_source(paths: Sequence[Path]) -> None:
    """No filesystem access. Reject mixed parents and conflicting named label sets.

    A selected folder is assumed to belong to one camera. Bare timestamps
    and numeric IDs cannot prove identity; they may coexist in that folder.
    """
    if len({p.parent for p in paths}) > 1:
        raise ValueError("recordings from different source folders cannot be mixed")
    labels = set()
    channels = set()
    for path in paths:
        match = analyze_recording_filename(path.name)
        if match is None:
            raise ValueError(f"invalid or ambiguous recording filename: {path.name}")
        named = set()
        for label in (match.prefix, match.suffix):
            if not label or (label.isascii() and label.isdigit()):
                continue
            named.add(label.casefold())
            channel = _CHANNEL.match(label)
            if channel:
                channels.add(int(channel.group(1) or channel.group(2)))
        if named:
            labels.add(frozenset(named))
    if len(channels) > 1 or len(labels) > 1:
        raise ValueError("conflicting camera or channel prefixes/suffixes; select a single camera source")
