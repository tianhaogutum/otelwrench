"""Strict camelCase OTLP/JSON log subset and deterministic value conversion."""

import base64
from dataclasses import dataclass
from decimal import Decimal
import json
import math
import re

from .strict import fail, obj, seq, string, uint


@dataclass(frozen=True)
class Value:
    kind: str
    raw: object

    def text(self):
        if self.kind == "empty":
            return ""
        if self.kind in ("stringValue", "bytesValue"):
            return self.raw
        return compact(self.raw)


def compact(value):
    """JSON strings without whitespace; numeric text follows ES6-style cutoffs."""
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + compact(v)
                              for k, v in sorted(value.items())) + "}"
    if isinstance(value, list):
        return "[" + ",".join(map(compact, value)) + "]"
    if type(value) is float:
        if value == 0:
            return "0"
        number = Decimal(repr(value))
        if 1e-6 <= abs(value) < 1e21:
            fixed = format(number, "f")
            return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
        mantissa, exponent = format(number.normalize(), "e").split("e")
        return mantissa + "e" + ("+" if int(exponent) >= 0 else "-") + str(abs(int(exponent)))
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def any_value(data, where, depth=0):
    if depth > 32:
        fail(where, "AnyValue nesting exceeds 32")
    obj(data, where, {"stringValue", "boolValue", "intValue", "doubleValue",
                      "arrayValue", "kvlistValue", "bytesValue"})
    if not data:
        return Value("empty", None)
    if len(data) != 1:
        fail(where, "AnyValue must have exactly one variant (or be empty)")
    kind, raw = next(iter(data.items()))
    if kind == "stringValue":
        string(raw, where)
    elif kind == "boolValue":
        if type(raw) is not bool:
            fail(where, "boolValue must be boolean")
    elif kind == "intValue":
        if not isinstance(raw, str) or not re.fullmatch(r"0|-?[1-9][0-9]*", raw):
            fail(where, "intValue must be a canonical decimal int64 string")
        raw = int(raw)
        if not -(2**63) <= raw < 2**63:
            fail(where, "intValue outside int64 range")
    elif kind == "doubleValue":
        if type(raw) not in (int, float):
            fail(where, "doubleValue must be a finite JSON number")
        try:
            raw = float(raw)
        except (OverflowError, ValueError):
            fail(where, "doubleValue outside finite float64 range")
        if not math.isfinite(raw):
            fail(where, "doubleValue must be finite")
    elif kind == "bytesValue":
        string(raw, where)
        try:
            decoded = base64.b64decode(raw, validate=True)
        except (ValueError, base64.binascii.Error):
            fail(where, "bytesValue must be canonical padded base64")
        if base64.b64encode(decoded).decode() != raw:
            fail(where, "bytesValue must be canonical padded base64")
    elif kind == "arrayValue":
        obj(raw, where, {"values"})
        raw = [any_value(x, f"{where}[{i}]", depth + 1).raw
               for i, x in enumerate(seq(raw.get("values", []), where))]
    else:
        obj(raw, where, {"values"})
        raw = {key: value.raw for key, value in attributes(raw.get("values", []), where, depth + 1)}
    return Value(kind, raw)


def attributes(data, where, depth=0):
    result, seen = [], set()
    for index, item in enumerate(seq(data, where)):
        loc = f"{where}[{index}]"
        obj(item, loc, {"key", "value"})
        key = string(item.get("key"), loc, nonempty=True)
        if key in seen:
            fail(loc, f"duplicate attribute {key!r}")
        seen.add(key)
        if "value" not in item:
            fail(loc, "missing AnyValue")
        result.append((key, any_value(item["value"], loc, depth)))
    return result


def flatten(path, value):
    if value.kind == "kvlistValue":
        if not value.raw:
            fail("/".join(path), "empty map attribute is unsupported (would vanish during flattening)")
        yield from flatten_raw(path, value.raw)
    else:
        yield path, value.text(), value.kind


def flatten_raw(path, raw):
    if isinstance(raw, dict):
        if not raw:
            fail("/".join(path), "empty map attribute is unsupported")
        for key, value in raw.items():
            yield from flatten_raw(path + [key], value)
    else:
        # Nested AnyValue leaves have been validated before this step.
        yield path, ("" if raw is None else raw if isinstance(raw, str) else compact(raw)), "nested"


def timestamp(value, where):
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]*", value):
        fail(where, "timestamp must be a canonical uint64 nanosecond string")
    if len(value) > 20 or int(value) >= 2**64:
        fail(where, "timestamp outside uint64 range")
    return value


def parse_logs(data):
    obj(data, "OTLP", {"resourceLogs"})
    if "resourceLogs" not in data:
        fail("OTLP", "missing resourceLogs")
    records, diagnostics = [], []

    def schema(container, loc):
        if "schemaUrl" in container:
            value = string(container["schemaUrl"], loc + ".schemaUrl")
            if value:
                diagnostics.append({"path": loc + ".schemaUrl", "value": value,
                                    "reason": "schema context is reported here, not emitted to push metadata"})

    for ri, resource_logs in enumerate(seq(data["resourceLogs"], "resourceLogs")):
        rloc = f"resourceLogs[{ri}]"
        obj(resource_logs, rloc, {"resource", "scopeLogs", "schemaUrl"})
        schema(resource_logs, rloc)
        resource = obj(resource_logs.get("resource", {}), rloc + ".resource",
                       {"attributes", "droppedAttributesCount"})
        resource_attrs = attributes(resource.get("attributes", []), rloc + ".resource.attributes")
        resource_dropped = uint(resource.get("droppedAttributesCount", 0), rloc + ".resource.droppedAttributesCount")
        if resource_dropped:
            diagnostics.append({"path": rloc + ".resource.droppedAttributesCount", "value": resource_dropped,
                                "reason": "attributes already lost upstream; count reported here, not emitted"})
        for si, scope_logs in enumerate(seq(resource_logs.get("scopeLogs", []), rloc + ".scopeLogs")):
            sloc = f"{rloc}.scopeLogs[{si}]"
            obj(scope_logs, sloc, {"scope", "logRecords", "schemaUrl"})
            schema(scope_logs, sloc)
            scope = obj(scope_logs.get("scope", {}), sloc + ".scope",
                        {"name", "version", "attributes", "droppedAttributesCount"})
            scope_attrs = attributes(scope.get("attributes", []), sloc + ".scope.attributes")
            fixed_scope = {}
            for field in ("name", "version"):
                value = string(scope.get(field, ""), sloc + ".scope." + field)
                if value:
                    fixed_scope["scope_" + field] = value
            count = uint(scope.get("droppedAttributesCount", 0), sloc + ".scope.droppedAttributesCount")
            if count:
                fixed_scope["scope_dropped_attributes_count"] = str(count)
                diagnostics.append({"path": sloc + ".scope.droppedAttributesCount", "value": count,
                                    "reason": "attributes already lost upstream; count also emitted as scope metadata"})
            for li, log in enumerate(seq(scope_logs.get("logRecords", []), sloc + ".logRecords")):
                loc = f"{sloc}.logRecords[{li}]"
                obj(log, loc, {"timeUnixNano", "observedTimeUnixNano", "severityNumber", "severityText",
                               "body", "attributes", "droppedAttributesCount", "flags", "traceId", "spanId", "eventName"})
                time = timestamp(log.get("timeUnixNano", "0"), loc + ".timeUnixNano")
                observed = timestamp(log.get("observedTimeUnixNano", "0"), loc + ".observedTimeUnixNano")
                if time == observed == "0":
                    fail(loc, "missing timestamp; offline preview cannot invent ingestion time")
                fixed = dict(fixed_scope)
                if time != "0" and observed != "0":
                    fixed["observed_timestamp"] = observed
                for field, target in (("severityText", "severity_text"), ("eventName", "event_name")):
                    value = string(log.get(field, ""), loc + "." + field)
                    if value:
                        fixed[target] = value
                for field, target in (("severityNumber", "severity_number"), ("flags", "flags"),
                                      ("droppedAttributesCount", "dropped_attributes_count")):
                    value = uint(log.get(field, 0), loc + "." + field)
                    if field == "severityNumber" and value > 24:
                        fail(loc, "severityNumber must be an integer from 0 to 24")
                    if value:
                        fixed[target] = str(value)
                        if field == "droppedAttributesCount":
                            diagnostics.append({"path": loc + ".droppedAttributesCount", "value": value,
                                                "reason": "attributes already lost upstream; count also emitted as log metadata"})
                for field, target, length in (("traceId", "trace_id", 32), ("spanId", "span_id", 16)):
                    value = string(log.get(field, ""), loc + "." + field)
                    if value:
                        if not re.fullmatch(r"[0-9a-fA-F]{" + str(length) + "}", value) or int(value, 16) == 0:
                            fail(loc, f"{field} must be {length} nonzero hex digits")
                        fixed[target] = value.lower()
                body = any_value(log.get("body", {}), loc + ".body")
                records.append({"id": loc, "timestamp": time if time != "0" else observed,
                                "timestampSource": "timeUnixNano" if time != "0" else "observedTimeUnixNano",
                                "line": body.text(), "bodyType": body.kind, "fixed": fixed,
                                "resource": resource_attrs, "scope": scope_attrs,
                                "log": attributes(log.get("attributes", []), loc + ".attributes")})
    return records, diagnostics
