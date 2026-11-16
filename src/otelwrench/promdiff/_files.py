"""Shared bounded binary reads with strict UTF-8 decoding."""
from pathlib import Path

from .parser import InputError


MAX_INPUT_BYTES = 64 * 1024 * 1024


def read_text(path: str | Path, *, max_bytes: int) -> str:
    """Read at most max_bytes+1 bytes; callers validate their own arguments."""
    try:
        with Path(path).open("rb") as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise InputError(f"{path}: input exceeds {max_bytes} byte limit")
        return data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise InputError(f"{path}: {exc}") from exc
