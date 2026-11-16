"""Bounded, opt-in gzip decoding before byte-oriented envelope parsing."""
import gzip
import io
import sys
import zlib

from .parser import ConversionError

DEFAULT_MAX_BYTES = 64 * 1024 * 1024
MAX_BYTE_BUDGET = sys.maxsize - 1  # Reserve one representable overflow-detection byte.


def validate_byte_budget(limit: int) -> int:
    if type(limit) is not int or not 1 <= limit <= MAX_BYTE_BUDGET:
        raise ConversionError(f"byte budget must be a positive integer in supported range 1..{MAX_BYTE_BUDGET}")
    return limit


def read_bounded(handle, limit: int) -> bytes:
    """Read through EOF or a validated budget plus one, without a budget-sized allocation."""
    remaining = limit + 1
    chunks = []
    would_block = "input read would block; use blocking binary input"
    while remaining:
        requested = min(remaining, 64 * 1024)
        try:
            chunk = handle.read(requested)
        except BlockingIOError as exc:
            raise ConversionError(would_block) from exc
        # Raw/buffered nonblocking streams can return None instead of raising.
        # Only an empty byte string proves EOF; available bytes may be a prefix.
        if chunk is None:
            raise ConversionError(would_block)
        # Check the type first: empty text and containers are not binary EOF.
        # Do not coerce custom stream results or leak their contents in errors.
        if not isinstance(chunk, bytes):
            raise ConversionError("binary input read must return bytes")
        # Subclasses can override both truthiness and len(); neither describes
        # the bytes retained by join. Use their actual extent for EOF/budgets.
        chunk_size = bytes.__len__(chunk)
        if chunk_size > requested:
            raise ConversionError("binary input read returned more bytes than requested")
        if chunk_size == 0:
            break
        chunks.append(chunk)
        remaining -= chunk_size
    return b"".join(chunks)


def decode_input(data: bytes, compression: str, max_input_bytes: int,
                 max_decompressed_bytes: int) -> bytes:
    for name, limit in (("max-input-bytes", max_input_bytes),
                        ("max-decompressed-bytes", max_decompressed_bytes)):
        try:
            validate_byte_budget(limit)
        except ConversionError as exc:
            raise ConversionError(f"--{name}: {exc}") from exc
    if not isinstance(data, bytes):
        raise ConversionError("input data must be bytes")
    if bytes.__len__(data) > max_input_bytes:
        raise ConversionError(f"input exceeds --max-input-bytes={max_input_bytes}")
    # Check the actual source extent before copying or decoding. Use the base
    # method to retain ordinary byte content without subclass conversion hooks,
    # truthiness, length or framing overrides reaching gzip/the parser.
    data = bytes.__bytes__(data)
    if compression == "none":
        return data
    if compression != "gzip":
        raise ConversionError(f"unsupported input compression: {compression!r}")
    if not data:
        raise ConversionError("invalid gzip input: missing header")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as handle:
            # Do not use gzip.decompress(): it allocates the entire expansion.
            # Reading one byte beyond the budget detects overflow, including
            # across concatenated members. If within budget, read through EOF
            # to validate every member's trailer before parsing or publication.
            decoded = read_bounded(handle, max_decompressed_bytes)
    except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
        raise ConversionError(f"invalid or truncated gzip input: {exc}") from exc
    if len(decoded) > max_decompressed_bytes:
        raise ConversionError(f"gzip expansion exceeds --max-decompressed-bytes={max_decompressed_bytes}")
    return decoded
