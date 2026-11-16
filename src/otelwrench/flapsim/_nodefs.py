# SPDX-License-Identifier: Apache-2.0
"""Node.js-style filesystem error messages (``ENOENT: no such file or directory, stat 'x'``).

Error text is part of the CLI contract (scripts match on the errno code), so
``OSError`` is reported the way the original Node.js implementation did.
"""
from __future__ import annotations

import errno

_DESCRIPTIONS = {
    "ENOENT": "no such file or directory",
    "ENOTDIR": "not a directory",
    "EEXIST": "file already exists",
    "EACCES": "permission denied",
    "EPERM": "operation not permitted",
    "EISDIR": "illegal operation on a directory",
    "ELOOP": "too many symbolic links encountered",
    "ENAMETOOLONG": "name too long",
    "EIO": "i/o error",
    "ENOSPC": "no space left on device",
    "EROFS": "read-only file system",
    "EXDEV": "cross-device link not permitted",
    "EBUSY": "resource busy or locked",
    "EINVAL": "invalid argument",
}


class NodeFsError(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def node_error(error: OSError, syscall: str, path: str, dest: str | None = None) -> NodeFsError:
    code = errno.errorcode.get(error.errno or 0, "UNKNOWN")
    description = _DESCRIPTIONS.get(code, (error.strerror or "unknown error").lower())
    target = f"'{path}'" if dest is None else f"'{path}' -> '{dest}'"
    return NodeFsError(f"{code}: {description}, {syscall} {target}", code)
