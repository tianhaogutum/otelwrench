"""Readable evidence views and diffs; never evaluate Collector rules here."""

from __future__ import annotations

import json
from typing import Any, Iterator

from deepdiff import DeepDiff
from jsonpointer import JsonPointer

from .inputs import ReplayError, SIGNAL_KEYS


ANY_VALUE_TYPES = frozenset(
    {
        "stringValue",
        "boolValue",
        "intValue",
        "doubleValue",
        "arrayValue",
        "kvlistValue",
        "bytesValue",
    }
)


def view(value: Any, key: str = "") -> Any:
    """Key OTLP attribute lists by name, preserving every AnyValue type wrapper."""
    if isinstance(value, dict):
        return {k: view(v, k) for k, v in value.items()}
    if isinstance(value, list):
        if (
            key in {"attributes", "filteredAttributes"}
            or key == "values"
            and all(isinstance(item, dict) and "key" in item and "value" in item for item in value)
            and value
        ):
            result = {}
            for item in value:
                if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                    raise ReplayError("invalid attribute entry in Collector output")
                name = item["key"]
                if name in result:
                    raise ReplayError(
                        f"duplicate attribute key {name!r} cannot be addressed unambiguously; "
                        "raw Collector output is retained"
                    )
                result[name] = view(item.get("value"))
            return result
        return [view(item) for item in value]
    return value


def json_path(parts: tuple) -> str:
    return "$" + "".join(
        f"[{part}]" if isinstance(part, int) else f"[{json.dumps(part, ensure_ascii=False)}]"
        for part in parts
    )


def json_pointer(parts: tuple) -> str:
    return JsonPointer.from_parts(parts).path


def leaves(value: Any, parts: tuple = ()) -> Iterator[tuple[tuple, Any]]:
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from leaves(item, parts + (key,))
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            yield from leaves(item, parts + (index,))
    else:
        yield parts, value


def equal(left: Any, right: Any) -> bool:
    return not DeepDiff(left, right, ignore_order=False)


def area(parts: tuple) -> str:
    special = next(
        (
            index
            for index, part in enumerate(parts)
            if part in {"attributes", "filteredAttributes", "body"}
        ),
        None,
    )
    if special is not None and parts[special] == "body":
        return "log_body"
    if special is not None:
        prefix = parts[:special]
        if "events" in prefix:
            return "event_attributes"
        if "links" in prefix:
            return "link_attributes"
        if "scope" in prefix:
            return "scope_attributes"
        if "resource" in prefix:
            return "resource_attributes"
        if "exemplars" in prefix:
            return "exemplar_attributes"
        return "record_attributes"
    if "status" in parts and parts[-1] == "message":
        return "status_message"
    if "scope" in parts:
        return "scope_metadata"
    if "events" in parts and parts[-1] == "name":
        return "event_name"
    if "spans" in parts and parts[-1] == "name":
        return "span_name"
    if "metrics" in parts and parts[-1] in {"name", "description", "unit"}:
        return "metric_metadata"
    return "other_field"


def inventory(before: dict, after: dict) -> list[dict]:
    old = dict(leaves(before))
    new = dict(leaves(after))
    rows = []
    for parts in sorted(old.keys() | new.keys(), key=json_path):
        row = {"path": json_path(parts), "pointer": json_pointer(parts), "area": area(parts)}
        if parts not in old:
            row.update(status="added", after=new[parts])
        elif parts not in new:
            row.update(status="removed", before=old[parts])
        else:
            status = "retained" if equal(old[parts], new[parts]) else "changed"
            row.update(status=status, before=old[parts], after=new[parts])
        rows.append(row)
    return rows


def differences(before: dict, after: dict) -> dict:
    return json.loads(DeepDiff(before, after, ignore_order=False, verbose_level=2).to_json())


def diagnostics(before: dict, after: dict) -> list[dict]:
    old = dict(leaves(before))
    result = []
    for parts, value in leaves(after):
        if "attributes" not in parts or area(parts) == "log_body":
            continue
        index = parts.index("attributes") + 1
        if index < len(parts) and str(parts[index]).startswith("redaction."):
            row = {
                "path": json_path(parts),
                "pointer": json_pointer(parts),
                "value": value,
                "present_before": parts in old,
                "source": "actual output attribute in redaction namespace; may be user-provided",
                "changed_in_this_stage": parts not in old or not equal(old[parts], value),
            }
            if parts in old:
                row["before"] = old[parts]
            result.append(row)
    return result


def ignored_observations(before: dict, after: dict, config: dict) -> list[dict]:
    """Evidence of configured exact-key bypass, not an invented runtime match trace."""
    keys = config.get("ignored_keys") or []
    old = dict(leaves(before))
    new = dict(leaves(after))
    observations = []
    for parts, value in old.items():
        if "attributes" not in parts or area(parts) == "log_body":
            continue
        index = parts.index("attributes")
        if index + 1 < len(parts) and parts[index + 1] in keys:
            observations.append(
                {
                    "path": json_path(parts),
                    "pointer": json_pointer(parts),
                    "key": parts[index + 1],
                    "area": area(parts),
                    "configured_rule": "ignored_keys",
                    "observed_unchanged": parts in new and equal(value, new[parts]),
                    "before": value,
                    "after": new.get(parts),
                    "attribution": "configuration plus observation; not an instrumented rule trace",
                }
            )
    return observations


def count_records(document: dict, signal: str) -> int:
    count = 0
    scopes = {"traces": "scopeSpans", "logs": "scopeLogs", "metrics": "scopeMetrics"}[signal]
    records = {"traces": "spans", "logs": "logRecords", "metrics": "metrics"}[signal]
    for resource in document.get(SIGNAL_KEYS[signal], []):
        for scope in resource.get(scopes, []):
            count += len(scope.get(records, []))
    return count


def contains_text(value: Any, needle: str) -> bool:
    if isinstance(value, dict):
        return any(needle in key or contains_text(item, needle) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_text(item, needle) for item in value)
    if isinstance(value, str):
        return needle in value
    return needle in json.dumps(value, ensure_ascii=False)


def find_text(value: Any, needle: str, parts: tuple = ()) -> Iterator[dict]:
    if isinstance(value, dict):
        for key, item in value.items():
            if needle in key:
                yield {
                    "path": json_path(parts + (key,)),
                    "pointer": json_pointer(parts + (key,)),
                    "area": area(parts + (key,)),
                    "match": "key",
                    "value": key,
                }
            yield from find_text(item, needle, parts + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from find_text(item, needle, parts + (index,))
    elif contains_text(value, needle):
        yield {
            "path": json_path(parts),
            "pointer": json_pointer(parts),
            "area": area(parts),
            "match": "value",
            "value": value,
        }


def _typed_values(value: Any, parts: tuple = ()) -> Iterator[tuple[tuple, str, Any]]:
    if isinstance(value, dict):
        if len(value) == 1:
            kind, payload = next(iter(value.items()))
            scalar = kind in {"stringValue", "bytesValue"} and isinstance(payload, str)
            scalar |= kind == "boolValue" and type(payload) is bool
            scalar |= kind in {"intValue", "doubleValue"} and type(payload) in {str, int, float}
            container = kind in {"arrayValue", "kvlistValue"} and isinstance(payload, dict)
            container = container and not (set(payload) - {"values"})
            if kind in ANY_VALUE_TYPES and (scalar or container):
                yield parts, kind, payload
        for key, item in value.items():
            yield from _typed_values(item, parts + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _typed_values(item, parts + (index,))


def type_coverage(before: dict, after: dict) -> dict:
    old = {parts: (kind, value) for parts, kind, value in _typed_values(before)}
    new = {parts: (kind, value) for parts, kind, value in _typed_values(after)}
    rows = []
    for kind in sorted(ANY_VALUE_TYPES):
        inputs = {parts: value for parts, (typ, value) in old.items() if typ == kind}
        outputs = {parts: value for parts, (typ, value) in new.items() if typ == kind}
        unchanged = sum(
            parts in outputs and equal(value, outputs[parts]) for parts, value in inputs.items()
        )
        rows.append(
            {
                "type": kind,
                "input_locations": len(inputs),
                "output_locations": len(outputs),
                "unchanged_input_locations": unchanged,
                "removed_or_retyped_input_locations": len(inputs.keys() - outputs.keys()),
            }
        )
    return {
        "types": rows,
        "unexercised_input_types": [row["type"] for row in rows if row["input_locations"] == 0],
        "meaning": "observed OTLP AnyValue locations; unchanged means survived this replay, "
        "not a universal statement about processor support",
    }
