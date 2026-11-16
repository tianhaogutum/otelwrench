# SPDX-License-Identifier: Apache-2.0
"""``otelwrench tracepack``: offline saved-trace conversion, bundling and verification.

The argument grammar is parsed by hand (not argparse) so that error text, error
precedence and exit codes stay identical to the original tool: every failure is one
JSON object on stderr, exit 1, or exit 2 for a strict-loss refusal.
"""
from __future__ import annotations

import errno
import os
import stat
import sys
import uuid
from collections.abc import Iterator
from typing import Any

from .bundle import BUNDLE_LIMIT, SOURCE_LIMIT, create_bundle, decode_utf8, extract_bundle, verified_bundle
from .convert import convert
from .errors import StrictLossError, TracepackError
from .formats import validate_formats
from .jsonfmt import UNDEFINED, canonical_json, js_number, stringify

PROG = "otelwrench tracepack"
HELP = f"""{PROG} — offline saved-trace conversion
Usage: {PROG} convert INPUT|- --to otlp|jaeger|zipkin [options]
       {PROG} bundle INPUT|- [--from auto|otlp|jaeger|zipkin] [--strict-loss] [--output FILE|-]
       {PROG} verify BUNDLE|- [--source SOURCE] [--strict-loss] [--report]
       {PROG} extract BUNDLE|- --output FILE [--strict-loss]
  --from auto|otlp|jaeger|zipkin  Input format (default: auto)
  --output FILE|-               Atomic file output or stdout (default: -)
  --strict-loss                 Reject reported losses (convert, bundle, verify or extract)
  --dry-run                     Validate and print report only; write no files
  --zipkin-error-tag            OTLP/Jaeger -> Zipkin: synthesize string error tag for ERROR
  --source SOURCE               Verify: also compare exact original bytes to a local regular file
  --report                      Verify: include complete replay-verified report in success JSON
  --help                        Show this help
Convert/bundle: loss report on stderr; trace/bundle JSON on stdout or --output.
Verify: success JSON on stdout, errors on stderr; replays offline (no authenticity claim).
Extract: verify first, then atomically restore original bytes; silent success, errors on stderr.
Source limit: 64 MiB. Bundle limit: 256 MiB. No network access or scripts.
"""

COMMANDS = ("convert", "bundle", "verify", "extract")
_ALLOWED = {
    "verify": ("--source", "--strict-loss", "--report", "--help"),
    "extract": ("--output", "--strict-loss", "--help"),
    "bundle": ("--from", "--output", "--strict-loss", "--help"),
    "convert": ("--from", "--to", "--output", "--strict-loss", "--dry-run", "--zipkin-error-tag", "--help"),
}
_VALUE_FLAGS = ("--from", "--to", "--output", "--source")

# libuv-style descriptions so I/O errors read like the original ("ENOENT: no such file ...").
_UV_MESSAGES = {
    errno.ENOENT: "no such file or directory", errno.EACCES: "permission denied",
    errno.EISDIR: "illegal operation on a directory", errno.ENOTDIR: "not a directory",
    errno.ELOOP: "too many symbolic links encountered", errno.EEXIST: "file already exists",
    errno.EPERM: "operation not permitted", errno.EROFS: "read-only file system",
    errno.ENOSPC: "no space left on device", errno.EXDEV: "cross-device link not permitted",
    errno.EBUSY: "resource busy or locked", errno.EMFILE: "too many open files",
    errno.ENAMETOOLONG: "name too long", errno.EINVAL: "invalid argument",
    errno.ENXIO: "no such device or address", errno.EIO: "i/o error",
}


def _io_error(error: OSError, syscall: str, path: str, dest: str | None = None) -> TracepackError:
    code = errno.errorcode.get(error.errno or 0, "EIO")
    description = _UV_MESSAGES.get(error.errno or 0) or (error.strerror or "unknown error").lower()
    target = f"'{path}' -> '{dest}'" if dest is not None else f"'{path}'"
    return TracepackError(f"{code}: {description}, {syscall} {target}")


# Indirections over the OS so tests can observe or refuse file access.
def _open(path: str, flags: int, mode: int = 0o777) -> int:
    return os.open(path, flags, mode)


def _lstat(path: str) -> os.stat_result:
    return os.lstat(path)


def _close(fd: int) -> None:
    os.close(fd)


def _file_chunks(fd: int) -> Iterator[bytes]:
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return
        yield chunk


def _stdin_chunks() -> Iterator[bytes]:
    stdin = sys.stdin
    if stdin is None:
        return
    stream = getattr(stdin, "buffer", stdin)
    reader = getattr(stream, "read1", None) or stream.read
    while True:
        chunk = reader(1024 * 1024)
        if not chunk:
            return
        yield chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")


class _Usage(TracepackError):
    pass


def parse_args(argv: list[str]) -> dict[str, Any] | None:
    """Return parsed options, or ``None`` when help was requested."""
    if len(argv) == 1 and argv[0] in ("--help", "-h"):
        return None
    if len(argv) == 2 and argv[0] in COMMANDS and argv[1] in ("--help", "-h"):
        return None
    if not argv or argv[0] not in COMMANDS or len(argv) < 2 or not argv[1] \
            or (argv[1].startswith("-") and argv[1] != "-"):
        raise _Usage("expected convert, bundle, verify or extract INPUT; use --help")
    result: dict[str, Any] = {"command": argv[0], "input": argv[1], "from": "auto", "output": "-",
                              "strictLoss": False, "dryRun": False}
    seen: set[str] = set()
    allowed = _ALLOWED[result["command"]]
    i = 2
    while i < len(argv):
        flag = argv[i]
        if flag in seen:
            raise _Usage(f"duplicate option {flag}")
        seen.add(flag)
        if flag not in allowed:
            raise _Usage(f"unknown option {flag} for {result['command']}")
        if flag == "--strict-loss":
            result["strictLoss"] = True
        elif flag == "--report":
            result["report"] = True
        elif flag == "--dry-run":
            result["dryRun"] = True
        elif flag == "--zipkin-error-tag":
            result["zipkinErrorTag"] = True
        elif flag == "--help":
            return None
        elif flag in _VALUE_FLAGS:
            i += 1
            value = argv[i] if i < len(argv) else ""
            if not value or value.startswith("--"):
                raise _Usage(f"missing value for {flag}")
            if flag == "--source" and value == "-":
                raise _Usage("--source requires a local regular file path, not stdin (use ./- for a file named -)")
            result[flag[2:]] = value
        i += 1
    # Finish syntactic parsing first, then check formats before any primary I/O.
    if result["command"] in ("convert", "bundle"):
        validate_formats(result["from"], "otlp" if result["command"] == "bundle" else result.get("to", UNDEFINED))
    # Only flag-known incompatibilities belong here; auto detection and tag
    # collisions still require the parsed source and are checked by convert.
    if result.get("zipkinErrorTag") and (result.get("to") != "zipkin" or result["from"] == "zipkin"):
        raise _Usage("--zipkin-error-tag: only supported for OTLP or Jaeger to Zipkin")
    if result["command"] == "extract" and result["output"] == "-":
        raise _Usage("extract requires --output FILE (a file path, not stdout)")
    return result


def _limit_error(limit: int) -> TracepackError:
    return TracepackError(f"input exceeds {js_number(limit / 1024 / 1024)} MiB limit")


def read_input(path: str, limit: int, kind: str = "input") -> bytes:
    regular_only = kind == "source"
    if regular_only:
        regular_error = "source must be a regular file, not a directory/symlink/device/FIFO"
    else:
        regular_error = f"{'bundle input' if kind == 'bundle' else 'input'} must be a regular file, not a directory/device/FIFO"
    if regular_only:
        try:
            info = _lstat(path)
        except OSError as error:
            raise _io_error(error, "lstat", path) from None
        if not stat.S_ISREG(info.st_mode):
            raise TracepackError(regular_error)
    # Primary paths may follow symlinks; source comparison paths may not. Open without
    # waiting for a FIFO writer, then validate the actual handle before consuming bytes.
    fd = -1
    if path != "-":
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0) \
            | (getattr(os, "O_NOFOLLOW", 0) if regular_only else 0)
        try:
            fd = _open(path, flags)
        except OSError as error:
            raise _io_error(error, "open", path) from None
    try:
        if path != "-":
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise TracepackError(regular_error)
            if info.st_size > limit:
                raise _limit_error(limit)
            chunks = _file_chunks(fd)
        else:
            chunks = _stdin_chunks()
        size = 0
        parts = []
        for chunk in chunks:
            size += len(chunk)
            if size > limit:
                raise _limit_error(limit)
            parts.append(chunk)
        return b"".join(parts)
    finally:
        if path != "-":
            _close(fd)


def atomic_write(path: str, data: str | bytes) -> None:
    target = os.path.abspath(path)
    tmp = os.path.join(os.path.dirname(target), f".{os.path.basename(target)}.{uuid.uuid4()}.tmp")
    # Refuse special files/symlinks: this CLI only replaces regular fixture files.
    try:
        info = _lstat(target)
    except FileNotFoundError:
        info = None
    except OSError as error:
        raise _io_error(error, "lstat", target) from None
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise TracepackError("output must be a regular file, not a directory/symlink/device")
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    fd: int | None = None
    try:
        try:
            fd = _open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
        except OSError as error:
            raise _io_error(error, "open", tmp) from None
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        try:
            os.rename(tmp, target)
        except OSError as error:
            raise _io_error(error, "rename", tmp, target) from None
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _emit(stream: Any, text: str) -> bool:
    """Write UTF-8 text; returns False on a closed pipe (EPIPE)."""
    try:
        stream.flush()
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            buffer.write(text.encode("utf-8"))
            buffer.flush()
        else:
            stream.write(text)
            stream.flush()
    except BrokenPipeError:
        return False
    return True


def _run(argv: list[str]) -> int:
    options = parse_args(argv)
    if options is None:
        return 0 if _emit(sys.stdout, HELP) else 1
    bundle_input = options["command"] in ("verify", "extract")
    data = read_input(options["input"], BUNDLE_LIMIT if bundle_input else SOURCE_LIMIT,
                      "bundle" if bundle_input else "input")
    if options["command"] == "extract":
        source = extract_bundle(decode_utf8(data), {"strictLoss": options["strictLoss"]})
        atomic_write(options["output"], source)
        return 0
    if options["command"] == "verify":
        raw, report, summary = verified_bundle(decode_utf8(data))
        if "source" in options:
            source = read_input(options["source"], SOURCE_LIMIT, "source")
            if source != raw:
                raise TracepackError("source does not match embedded original bytes")
            summary["sourceMatches"] = True
        if options["strictLoss"] and report["losses"]:
            raise StrictLossError(report)
        if options.get("report"):
            summary["report"] = report
        return 0 if _emit(sys.stdout, stringify(summary)) else 1
    if options["command"] == "bundle":
        output: Any = create_bundle(data, options)
        report = output["lossReport"]
    else:
        result = convert(decode_utf8(data), options)
        output, report = result["output"], result["report"]
    status = 0
    if not options["dryRun"]:
        text = canonical_json(output) + "\n" if options["command"] == "bundle" else stringify(output)
        try:
            if options["output"] == "-":
                if not _emit(sys.stdout, text):
                    status = 1
            else:
                atomic_write(options["output"], text)
        except Exception as error:
            error.report = report  # type: ignore[attr-defined]
            raise
    _emit(sys.stderr, stringify(report))
    return status


def main(argv: list[str] | None = None) -> int:
    """Run ``otelwrench tracepack``; returns 0, 1 (error) or 2 (strict-loss refusal)."""
    if argv is None:
        argv = sys.argv[1:]
    try:
        return _run(list(argv))
    except Exception as error:  # noqa: BLE001 - every failure becomes one JSON error object
        message = str(error)
        if isinstance(error, OSError) and not isinstance(error, TracepackError):
            message = str(_io_error(error, "write", getattr(error, "filename", None) or ""))
        envelope: dict[str, Any] = {"error": message}
        report = getattr(error, "report", None)
        if report:
            envelope["report"] = report
        _emit(sys.stderr, stringify(envelope))
        return 2 if isinstance(error, StrictLossError) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
