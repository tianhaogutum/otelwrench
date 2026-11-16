# SPDX-License-Identifier: Apache-2.0
"""CLI-only ``--manifest`` path selection. Report validation and serializers stay shared."""
from __future__ import annotations

import os
from typing import Any

from ._js import is_well_formed, js_eq
from .validation import FlapsimError


def validate_manifest_path(path: Any) -> None:
    if (not isinstance(path, str) or not path or path.startswith("-") or "\0" in path
            or not is_well_formed(path)):
        raise FlapsimError("--manifest: expected a nonempty local JSON file path "
                           "(no leading hyphen, NUL or unpaired surrogate)")


def resolve_path(base_dir: str, path: str) -> str:
    """``path.resolve(base, path)``: absolute, lexically normalized."""
    return os.path.normpath(os.path.join(os.path.abspath(base_dir), path))


# manifest_path is the canonical file path, so even a final-component symlink
# anchors relative report names at the target directory, independent of cwd.
def report_set_manifest_paths(value: Any, manifest_path: str) -> list:
    fields = ["schemaVersion", "baseline", "candidates"]
    if not isinstance(value, dict) or len(value) != len(fields) or any(key not in fields for key in value):
        raise FlapsimError("--manifest: expected exactly schemaVersion, baseline and candidates fields")
    if not js_eq(value["schemaVersion"], 1):
        raise FlapsimError("--manifest: expected schemaVersion 1")
    candidates = value["candidates"]
    if not isinstance(candidates, list) or len(candidates) < 1 or len(candidates) > 255:
        raise FlapsimError("--manifest: expected candidates array of 1 to 255 report paths (2 to 256 total reports)")
    paths = [value["baseline"], *candidates]
    for i, path in enumerate(paths):
        if not isinstance(path, str) or not path or "\0" in path or not is_well_formed(path):
            where = "baseline" if i == 0 else f"candidates[{i - 1}]"
            raise FlapsimError(f"--manifest: {where}: expected a nonempty path string without NUL or unpaired surrogates")
    return [resolve_path(os.path.dirname(manifest_path), path) for path in paths]
