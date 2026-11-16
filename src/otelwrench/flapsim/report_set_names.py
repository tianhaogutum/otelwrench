# SPDX-License-Identifier: Apache-2.0
"""CLI-only ``--names`` display metadata. Never used as report identity or replay input."""
from __future__ import annotations

from typing import Any

from ._js import is_well_formed, js_eq, utf8_length
from .validation import FlapsimError


def validate_names_path(path: Any) -> None:
    if (not isinstance(path, str) or not path or path.startswith("-") or "\0" in path
            or not is_well_formed(path)):
        raise FlapsimError("--names: expected a nonempty local JSON file path "
                           "(no leading hyphen, NUL or unpaired surrogate)")


def report_set_names(value: Any, candidate_count: int) -> dict:
    fields = ["schemaVersion", "baseline", "candidates"]
    if not isinstance(value, dict) or len(value) != len(fields) or any(key not in fields for key in value):
        raise FlapsimError("--names: expected exactly schemaVersion, baseline and candidates fields")
    if not js_eq(value["schemaVersion"], 1):
        raise FlapsimError("--names: expected schemaVersion 1")
    candidates = value["candidates"]
    if not isinstance(candidates, list) or len(candidates) < 1 or len(candidates) > 255:
        raise FlapsimError("--names: expected candidates array of 1 to 255 names")
    if len(candidates) != candidate_count:
        raise FlapsimError("--names: candidate name count must match input candidate report count")
    for i, name in enumerate([value["baseline"], *candidates]):
        if (not isinstance(name, str) or not name or not is_well_formed(name) or "\0" in name
                or utf8_length(name) > 256):
            where = "baseline" if i == 0 else f"candidates[{i - 1}]"
            raise FlapsimError(f"--names: {where}: expected 1 to 256 UTF-8 bytes without NUL or unpaired surrogates")
    # Preserve Unicode, whitespace and duplicates verbatim; index is authoritative.
    return {"baseline": value["baseline"], "candidates": list(candidates)}


# The CLI supplies only a freshly verified summary and validated mapping.
def name_report_set_summary(result: dict, names: dict) -> dict:
    return {**result, "baseline": {**result["baseline"], "displayName": names["baseline"]},
            "candidates": [{**c, "displayName": names["candidates"][i]} for i, c in enumerate(result["candidates"])]}
