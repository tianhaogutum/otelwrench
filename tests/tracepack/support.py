# SPDX-License-Identifier: Apache-2.0
"""Shared helpers for the tracepack suite (ported from the original test-support)."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import stat
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from otelwrench.tracepack import canonical_json
from otelwrench.tracepack import cli as tp_cli

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
EXAMPLES = HERE.parents[1] / "examples" / "tracepack"
TID = "abcdef0123456789abcdef0123456789"
SID = "1111111111111111"
SOURCE_LIMIT = 64 * 1024 * 1024
BUNDLE_LIMIT = 256 * 1024 * 1024

# Shared byte fixture: BOM, CRLF, non-ASCII and an unsafe integer token.
ORIGINAL = ('﻿\r\n[ {"traceId":"11111111111111111111111111111111","id":"2222222222222222",'
            '"name":"café/雪","timestamp":9007199254740993,"duration":1} ]\r\n').encode()
RAW_WITH_EXTRA = ('﻿\r\n[ {"traceId":"11111111111111111111111111111111","id":"2222222222222222",'
                  '"name":"café/雪","timestamp":9007199254740993,"duration":1EXTRA} ]\r\n')
LOSSY = RAW_WITH_EXTRA.replace("EXTRA", ',"mystery":"do not echo","debug":true').encode()
LOSSY_REPORT = {"from": "zipkin", "to": "otlp", "spanCount": 1, "losses": [
    {"path": '$[0]["debug"]', "reason": "unsupported field dropped"},
    {"path": '$[0]["mystery"]', "reason": "unsupported field dropped"},
]}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def clone_fixture(name: str) -> Any:
    return json.loads(fixture(name))


def example(name: str) -> str:
    return (EXAMPLES / name).read_text(encoding="utf-8")


def simple_otlp() -> dict[str, Any]:
    return {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "web"}}]},
                               "scopeSpans": [{"spans": [{
                                   "traceId": TID, "spanId": SID, "name": "request",
                                   "startTimeUnixNano": "1700000000000000000",
                                   "endTimeUnixNano": "1700000000001000000"}]}]}]}


def span(doc: Any) -> Any:
    return doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def jsdumps(value: Any) -> str:
    """``JSON.stringify(value)`` for the plain values used in these tests."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def json2(value: Any) -> str:
    """``JSON.stringify(value, null, 2) + '\\n'``."""
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def wire(bundle: Any) -> str:
    return canonical_json(bundle) + "\n"


def sha(data: str | bytes) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def unb64(data: str) -> bytes:
    return base64.b64decode(data)


def deepcopy(value: Any) -> Any:
    return copy.deepcopy(value)


def same_float(a: Any, b: float) -> bool:
    """``Object.is`` for numbers (distinguishes -0 and 0)."""
    return isinstance(a, float) and struct.pack(">d", a) == struct.pack(">d", b)


class _Out:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, text: str) -> int:
        self.buffer.write(text.encode("utf-8"))
        return len(text)

    def flush(self) -> None:
        pass


class _In:
    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


def run(args: list[str], input: str | bytes | None = None, hooks: dict[str, Any] | None = None,
        cwd: str | os.PathLike[str] | None = None) -> SimpleNamespace:
    """Run the CLI in-process with byte-exact stdin/stdout/stderr and optional I/O hooks."""
    data = input.encode("utf-8") if isinstance(input, str) else (input or b"")
    saved = sys.stdin, sys.stdout, sys.stderr
    saved_hooks = {name: getattr(tp_cli, name) for name in (hooks or {})}
    old_cwd = os.getcwd() if cwd is not None else None
    out, err = _Out(), _Out()
    try:
        if cwd is not None:
            os.chdir(cwd)
        for name, value in (hooks or {}).items():
            setattr(tp_cli, name, value)
        sys.stdin, sys.stdout, sys.stderr = _In(data), out, err  # type: ignore[assignment]
        status = tp_cli.main([str(a) for a in args])
    finally:
        sys.stdin, sys.stdout, sys.stderr = saved
        for name, value in saved_hooks.items():
            setattr(tp_cli, name, value)
        if old_cwd is not None:
            os.chdir(old_cwd)
    return SimpleNamespace(status=status, stdout=out.buffer.getvalue().decode("utf-8"),
                           stderr=err.buffer.getvalue().decode("utf-8"))


def run_process(args: list[str], stdin: Any = None, input: bytes | None = None) -> SimpleNamespace:
    """Run ``python -m otelwrench.tracepack`` as a real process (for FIFO stdin cases)."""
    kwargs: dict[str, Any] = {"input": input} if input is not None else {"stdin": stdin}
    result = subprocess.run([sys.executable, "-m", "otelwrench.tracepack", *map(str, args)],
                            capture_output=True, timeout=120, **kwargs)
    return SimpleNamespace(status=result.returncode, stdout=result.stdout.decode("utf-8"),
                           stderr=result.stderr.decode("utf-8"))


def refuse(message: str):
    def hook(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(message)
    return hook


def refusing_stdin(message: str):
    def hook() -> Any:
        raise RuntimeError(message)
        yield b""  # pragma: no cover
    return hook


def no_access_hooks(message: str = "unexpected file access", stdin_message: str = "unexpected stdin access"):
    """Equivalent of the original fs.open/fs.lstat/process.stdin trap."""
    return {"_open": refuse(message), "_lstat": refuse(message), "_stdin_chunks": refusing_stdin(stdin_message)}


class RefusalGuard:
    """Trap destination/source inspection, reading nonregular handles and leaked handles."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(path)
        self.opened = False
        self.closed = False

    def hooks(self) -> dict[str, Any]:
        real_open, real_close = os.open, os.close

        def _open(path: str, flags: int, mode: int = 0o777) -> int:
            if path != self.path:
                raise RuntimeError("unexpected source/destination open")
            fd = real_open(path, flags, mode)
            self.opened = True
            return fd

        def _close(fd: int) -> None:
            real_close(fd)
            self.closed = True

        def _file_chunks(fd: int):
            raise RuntimeError("nonregular input read")
            yield b""  # pragma: no cover

        return {"_open": _open, "_close": _close, "_lstat": refuse("unexpected path inspection"),
                "_file_chunks": _file_chunks}

    def run(self, args: list[str], input: bytes | None = None) -> SimpleNamespace:
        result = run(args, input, self.hooks())
        if self.opened and not self.closed:
            result.status = 99
        self.opened = self.closed = False
        return result


def growing_chunks(count: int):
    def chunks(*_args: Any):
        chunk = bytes(1024 * 1024)
        for _ in range(count):
            yield chunk
    return chunks


def snapshot(directory: str | os.PathLike[str], exclude: tuple[str, ...] = (), *, small_only: bool = False,
             with_size: bool = False) -> list[dict[str, Any]]:
    entries = []
    for name in sorted(os.listdir(directory)):
        if name in exclude:
            continue
        path = os.path.join(directory, name)
        info = os.lstat(path)
        regular = stat.S_ISREG(info.st_mode)
        read = regular and (not small_only or info.st_size < 1024)
        entry = {"name": name, "ino": info.st_ino, "mode": info.st_mode, "mtime": info.st_mtime_ns,
                 "bytes": Path(path).read_bytes() if read else None,
                 "link": os.readlink(path) if stat.S_ISLNK(info.st_mode) else None}
        if with_size:
            entry["size"] = info.st_size
        entries.append(entry)
    return entries


def mode(path: str | os.PathLike[str]) -> int:
    return os.lstat(path).st_mode & 0o777


def ino(path: str | os.PathLike[str]) -> int:
    return os.lstat(path).st_ino


def sparse(path: str | os.PathLike[str], size: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        os.ftruncate(fd, size)
    finally:
        os.close(fd)
