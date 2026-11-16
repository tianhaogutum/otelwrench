# SPDX-License-Identifier: Apache-2.0
"""Strict, bounded JSON selections for one reviewed comparison pair."""
from __future__ import annotations

from .core import Dimension, InputError, array, obj, parse_dimensions, strict_loads, string


MAX_CONFIG_BYTES = 65_536


def parse_dimension_config(data: bytes) -> tuple[tuple[Dimension, ...], tuple[Dimension, ...]]:
    """Return canonical baseline/candidate dimensions; never merge or infer fields."""
    if len(data) > MAX_CONFIG_BYTES:
        raise InputError(f"dimension configuration exceeds byte cap {MAX_CONFIG_BYTES}")
    root = obj(strict_loads(data, context="dimension configuration JSON"), "$",
               {"schema_version", "baseline", "candidate"})
    if type(root.get("schema_version")) is not int or root["schema_version"] != 1:
        raise InputError("$.schema_version: expected JSON integer 1")
    result = []
    for side in ("baseline", "candidate"):
        where = f"$.{side}"
        entries = array(root.get(side), where)
        if len(entries) > 32:
            raise InputError(f"{where}: at most 32 custom dimensions supported")
        specs = []
        for i, entry in enumerate(entries):
            path = f"{where}[{i}]"
            entry = obj(entry, path, {"source", "key", "label"})
            source = string(entry.get("source"), path + ".source")
            if source not in ("resource", "span"):
                raise InputError(f"{path}.source: expected resource or span")
            key = string(entry.get("key"), path + ".key", nonempty=True)
            if "=" in key:
                raise InputError(f"{path}.key: must not contain '='")
            label = string(entry.get("label"), path + ".label")
            specs.append(f"{source}:{key}={label}")
        try:
            result.append(parse_dimensions(specs))
        except InputError as exc:
            raise InputError(f"{where}: {exc}") from exc
    return result[0], result[1]
