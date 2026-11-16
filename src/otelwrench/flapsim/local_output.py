# SPDX-License-Identifier: Apache-2.0
"""CLI-only ``--output`` destination handling. All validation/formatting precedes this call."""
from __future__ import annotations

import os
import stat as stat_module
import types
import uuid
from typing import Any

from ._nodefs import node_error
from .validation import FlapsimError

# Filesystem primitives used here, as one patchable namespace so tests can
# inject failures at exactly one OS boundary.
fs = types.SimpleNamespace(open=os.open, write=os.write, close=os.close, rename=os.rename,
                           unlink=os.unlink, lstat=os.lstat, stat=os.stat)


def validate_output_path(path: Any) -> None:
    if (not isinstance(path, str) or not path or path.startswith("-") or "\0" in path
            or path.endswith(os.sep)):
        raise FlapsimError("--output: expected a nonempty local file path "
                           "(no leading hyphen or trailing separator; stdout is the default)")


def realpath(path: str) -> str:
    """``fs.realpathSync``: canonical path; raises a Node-style error if absent."""
    try:
        return os.path.realpath(path, strict=True)
    except OSError as error:
        raise node_error(error, "lstat", error.filename or path) from None


def _target_stat(path: str) -> os.stat_result | None:
    try:
        result = fs.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise node_error(error, "lstat", path) from None
    if not stat_module.S_ISREG(result.st_mode):
        raise FlapsimError("--output: expected an absent or regular file; symlinks and special targets are unsupported")
    return result


def _check_aliases(target: str, current: os.stat_result | None, inputs: list) -> None:
    for item in inputs:
        real = realpath(item)
        try:
            source = fs.stat(item)
        except OSError as error:
            raise node_error(error, "stat", item) from None
        if target == real or (current is not None and current.st_dev == source.st_dev and current.st_ino == source.st_ino):
            raise FlapsimError("--output: destination aliases an input report, manifest or names metadata")


def _same_target(a: os.stat_result | None, b: os.stat_result | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return all(getattr(a, key) == getattr(b, key) for key in
               ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns"))


def _message(error: BaseException) -> str:
    return str(error)


# A portable rename is atomic, but is not a compare-and-swap. This assumes
# trusted directories; the recheck catches observed changes, not hostile races.
def write_local_output(path: str, inputs: list, output: str) -> None:
    validate_output_path(path)
    parent = realpath(os.path.dirname(path) or ".")
    target = os.path.normpath(os.path.join(parent, os.path.basename(path)))
    before = _target_stat(target)
    _check_aliases(target, before, inputs)
    temporary = None
    fd = None
    try:
        proposed = os.path.join(parent, f".alert-flapsim-{uuid.uuid4()}.tmp")
        try:
            fd = fs.open(proposed, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TRUNC, 0o600)
        except OSError as error:
            raise node_error(error, "open", proposed) from None
        temporary = proposed  # Own only files successfully created exclusively.
        data = output.encode("utf-8")
        view = memoryview(data)
        try:
            while view:
                written = fs.write(fd, view)
                view = view[written:]
        except OSError as error:
            raise node_error(error, "write", proposed) from None
        try:
            fs.close(fd)
        except OSError as error:
            raise node_error(error, "close", proposed) from None
        fd = None
        current = _target_stat(target)
        _check_aliases(target, current, inputs)
        if not _same_target(before, current):
            raise FlapsimError("--output: destination changed before rename")
        try:
            fs.rename(temporary, target)
        except OSError as error:
            raise node_error(error, "rename", temporary, target) from None
        temporary = None
    except Exception as error:
        cleanup = []
        if fd is not None:
            try:
                fs.close(fd)
            except Exception as e:  # noqa: BLE001 - report every cleanup failure
                cleanup.append(f"close temporary file: {_message(e)}")
        if temporary is not None:
            try:
                fs.unlink(temporary)
            except Exception as e:  # noqa: BLE001
                cleanup.append(f"remove temporary file {temporary}: {_message(e)}")
        if cleanup:
            raise FlapsimError(f"{_message(error)}; cleanup failed: {'; '.join(cleanup)}") from error
        raise
