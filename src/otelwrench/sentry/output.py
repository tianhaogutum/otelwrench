"""Publish a complete directory using an atomic no-replace rename (Linux renameat2, macOS renamex_np)."""
import ctypes
import errno
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from .parser import ConversionError

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1  # Linux renameat2 flag
_RENAME_EXCL = 0x4  # macOS renamex_np flag


def _rename_new(source, destination):
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            rename = libc.renameat2
        except AttributeError as exc:
            raise ConversionError("libc has no renameat2; atomic no-replace publication is unavailable") from exc
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(_AT_FDCWD, os.fsencode(source), _AT_FDCWD, os.fsencode(destination), _RENAME_NOREPLACE)
    elif sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            rename = libc.renamex_np
        except AttributeError as exc:
            raise ConversionError("libc has no renamex_np; atomic no-replace publication is unavailable") from exc
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(os.fsencode(source), os.fsencode(destination), _RENAME_EXCL)
    else:
        raise ConversionError("atomic publication currently requires Linux renameat2 or macOS renamex_np; "
                              "use --dry-run for inspection")
    if result != 0:
        code = ctypes.get_errno()
        if code in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, getattr(errno, "ENOTSUP", errno.EOPNOTSUPP)}:
            raise ConversionError("filesystem/kernel does not support atomic no-replace directory publication")
        raise OSError(code, os.strerror(code), str(destination))


def write_output(documents, output_dir):
    output = Path(os.path.abspath(output_dir))
    if os.path.lexists(output):
        raise ConversionError(f"output directory already exists: {output}")
    if not output.parent.is_dir():
        raise ConversionError(f"output parent directory does not exist: {output.parent}")
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    try:
        for name in ("logs.json", "traces.json", "manifest.json"):
            with (staging / name).open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(documents[name], handle, ensure_ascii=False, allow_nan=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        _rename_new(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return output
