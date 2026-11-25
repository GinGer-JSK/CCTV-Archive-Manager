"""Conservative one-folder policy; prefixes are hints, not device identities."""
import re
from pathlib import Path
from typing import Sequence

from .filename_parser import FILENAME_PATTERN

_CHANNEL = re.compile(r"^(?:\[CH([0-9]+)\]|CH([0-9]+)(?=[ _-]|$))", re.IGNORECASE)


def validate_single_source(paths: Sequence[Path]) -> None:
    """No filesystem access. Reject mixed parents and conflicting named labels.

    A selected folder is assumed to belong to one camera. Bare timestamps
    and numeric IDs cannot prove identity; they may coexist in that folder.
    """
    if len({p.parent for p in paths}) > 1:
        raise ValueError("recordings from different source folders cannot be mixed")
    labels = set()
    channels = set()
    for path in paths:
        match = FILENAME_PATTERN.fullmatch(path.name)
        if match is None:
            continue  # caller validates timestamp and format
        prefix = match.group("prefix")
        if not prefix or (prefix.isascii() and prefix.isdigit()):
            continue
        labels.add(prefix.casefold())
        channel = _CHANNEL.match(prefix)
        if channel:
            channels.add(int(channel.group(1) or channel.group(2)))
    if len(channels) > 1 or len(labels) > 1:
        raise ValueError("conflicting camera or channel prefixes; select a single camera source")
