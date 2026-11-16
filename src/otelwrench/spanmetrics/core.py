# SPDX-License-Identifier: Apache-2.0
"""Strict OTLP subset validation and integer-nanosecond aggregation."""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from . import __version__


class InputError(ValueError):
    """An unsupported, malformed, conflicting, or over-budget input."""


BASE_DIMENSIONS = (
    {"source": "resource", "key": "service.name", "label": "service_name"},
    {"source": "span", "key": "name", "label": "span_name"},
    {"source": "span", "key": "kind", "label": "span_kind"},
    {"source": "span", "key": "status.code", "label": "status_code"},
)
RESERVED_LABELS = {d["label"] for d in BASE_DIMENSIONS} | {"le"}
UINT64_MAX = 2**64 - 1
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1
METRIC_LOSSES = [
    "Only allowlisted attributes become dimensions; all others are omitted from metrics.",
    "IDs, parentage, scope/resource metadata, events, links, trace state, flags, status messages "
    "and dropped counts do not become metrics; retained metadata is used for duplicate comparison.",
    "Each unique span contributes once, including children and every span kind; calls are span counts.",
    "Capture counts cannot infer traffic rate or compensate for sampling or missing telemetry.",
    "Prometheus consumers use floating point; exposition preserves exact decimal sums, consumers may round.",
]


def obj(value: Any, where: str, allowed: set[str]) -> dict:
    if not isinstance(value, dict):
        raise InputError(f"{where}: expected object")
    extra = set(value) - allowed
    if extra:
        raise InputError(f"{where}: unsupported fields: {', '.join(sorted(extra))}")
    return value


def array(value: Any, where: str) -> list:
    if not isinstance(value, list):
        raise InputError(f"{where}: expected array")
    return value


def string(value: Any, where: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise InputError(f"{where}: expected {'nonempty ' if nonempty else ''}string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InputError(f"{where}: invalid Unicode surrogate") from exc
    return value


def integer(value: Any, where: str, low: int, high: int, *, decimal_string=False) -> int:
    pattern = r"(0|[1-9][0-9]*)" if low >= 0 else r"-?(0|[1-9][0-9]*)"
    if decimal_string and isinstance(value, str) and re.fullmatch(pattern, value):
        if len(value) > 21:
            raise InputError(f"{where}: integer outside range [{low}, {high}]")
        value = int(value)
    if type(value) is not int or not low <= value <= high:
        raise InputError(f"{where}: expected integer in [{low}, {high}]")
    return value


def ident(value: Any, where: str, length: int, *, optional=False) -> str:
    if optional and value == "":
        return ""
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", value):
        raise InputError(f"{where}: expected {length} hexadecimal characters")
    if int(value, 16) == 0:
        raise InputError(f"{where}: all-zero ID is invalid")
    return value.lower()


def any_value(value: Any, where: str, depth: int = 0) -> tuple[str, Any]:
    if depth > 16:
        raise InputError(f"{where}: attribute nesting exceeds 16")
    value = obj(value, where, {"stringValue", "boolValue", "intValue", "doubleValue",
                               "bytesValue", "arrayValue", "kvlistValue"})
    if len(value) != 1:
        raise InputError(f"{where}: AnyValue needs exactly one typed value")
    kind, data = next(iter(value.items()))
    if kind == "stringValue":
        data = string(data, where)
    elif kind == "boolValue":
        if type(data) is not bool:
            raise InputError(f"{where}: boolValue must be boolean")
    elif kind == "intValue":
        data = integer(data, where, INT64_MIN, INT64_MAX, decimal_string=True)
    elif kind == "doubleValue":
        if type(data) not in (int, Decimal) or not Decimal(data).is_finite():
            raise InputError(f"{where}: only finite numeric doubleValue is supported")
        if abs(data) > Decimal("1.7976931348623157e308"):
            raise InputError(f"{where}: doubleValue exceeds finite float64 range")
        if data and Decimal(data).adjusted() < -324:
            raise InputError(f"{where}: doubleValue magnitude is below supported range 1e-324")
    elif kind == "bytesValue":
        string(data, where)
        try:
            base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise InputError(f"{where}: invalid base64 bytesValue") from exc
    elif kind == "arrayValue":
        data = obj(data, where, {"values"})
        data = [any_value(v, where, depth + 1) for v in array(data.get("values", []), where)]
    else:
        data = obj(data, where, {"values"})
        data = attributes(data.get("values", []), where, depth + 1)
    return kind, data


def attributes(value: Any, where: str, depth: int = 0) -> dict:
    result = {}
    for i, item in enumerate(array(value, where)):
        path = f"{where}[{i}]"
        item = obj(item, path, {"key", "value"})
        key = string(item.get("key"), path + ".key", nonempty=True)
        if key in result:
            raise InputError(f"{path}: duplicate attribute key {key!r}")
        result[key] = any_value(item.get("value"), path + ".value", depth)
    return result


def metadata(value: Any, where: str, *, scope=False) -> dict:
    allowed = {"attributes", "droppedAttributesCount"}
    if scope:
        allowed |= {"name", "version"}
    value = obj(value, where, allowed)
    if scope:
        for key in ("name", "version"):
            if key in value:
                string(value[key], f"{where}.{key}")
    integer(value.get("droppedAttributesCount", 0), where + ".droppedAttributesCount", 0, 2**32 - 1)
    return attributes(value.get("attributes", []), where + ".attributes")


def schema(value: dict, where: str) -> None:
    if "schemaUrl" in value:
        string(value["schemaUrl"], where + ".schemaUrl")


@dataclass(frozen=True)
class Dimension:
    source: str
    key: str
    label: str


def parse_dimensions(specs: list[str]) -> tuple[Dimension, ...]:
    dimensions = []
    labels = set(RESERVED_LABELS)
    sources = set()
    for spec in specs:
        match = re.fullmatch(r"(resource|span):([^=]+)=([a-zA-Z_][a-zA-Z0-9_]*)", spec)
        if not match:
            raise InputError("dimension must be resource:KEY=LABEL or span:KEY=LABEL")
        source, key, label = match.groups()
        if label in labels or label.startswith("__"):
            raise InputError(f"dimension label collision/reserved name: {label}")
        if (source, key) in sources:
            raise InputError(f"duplicate dimension source: {source}:{key}")
        if source == "resource" and key == "service.name":
            raise InputError("service.name is already a built-in dimension")
        dimensions.append(Dimension(source, key, label))
        labels.add(label)
        sources.add((source, key))
    if len(dimensions) > 32:
        raise InputError("at most 32 custom dimensions supported")
    return tuple(sorted(dimensions, key=lambda d: d.label))


def parse_buckets(spec: str) -> tuple[Decimal, ...]:
    tokens = spec.split(",")
    if not 1 <= len(tokens) <= 100:
        raise InputError("expected 1..100 finite histogram bounds")
    result = []
    for token in tokens:
        token = token.strip()
        if len(token) > 64 or not re.fullmatch(r"[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", token):
            raise InputError("buckets must be finite decimal seconds")
        exponent = re.search(r"[eE]([+-]?[0-9]+)$", token)
        if exponent and abs(int(exponent[1])) > 30:
            raise InputError("bucket exponent must be within -30..30")
        value = Decimal(token)
        if not value.is_finite() or value < 0 or (value and not Decimal("1e-30") <= value <= Decimal("1e30")):
            raise InputError("bucket must be nonnegative and within 1e-30..1e30 (or zero)")
        if result and value <= result[-1]:
            raise InputError("histogram bounds must be strictly increasing")
        result.append(value)
    return tuple(result)


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def seconds(ns: int) -> str:
    whole, fraction = divmod(ns, 1_000_000_000)
    return f"{whole}.{fraction:09d}".rstrip("0").rstrip(".") if fraction else str(whole)


def dimension_value(value: tuple[str, Any] | None, where: str) -> str:
    if value is None:
        return "missing"
    kind, data = value
    if kind == "stringValue":
        return "s:" + data
    if kind == "boolValue":
        return "b:" + str(data).lower()
    if kind == "intValue":
        return "i:" + str(data)
    if kind == "doubleValue":
        return "d:" + decimal_text(Decimal(data))
    raise InputError(f"{where}: dimensions support only string/bool/int/finite double attributes")


@dataclass(frozen=True)
class Config:
    buckets: tuple[Decimal, ...] = field(default_factory=lambda: parse_buckets("0.1,0.5,1"))
    dimensions: tuple[Dimension, ...] = ()
    max_series: int = 10_000
    max_spans: int = 100_000
    max_input_bytes: int = 64 * 1024 * 1024
    max_line_bytes: int = 1024 * 1024
    input_format: str = "json"

    def __post_init__(self):
        # Validate library callers too, not just the CLI.
        canonical = parse_buckets(",".join(str(x) for x in self.buckets))
        if canonical != self.buckets:
            raise InputError("invalid bucket configuration")
        expected = parse_dimensions([f"{d.source}:{d.key}={d.label}" for d in self.dimensions])
        if expected != self.dimensions:
            raise InputError("dimensions must be sorted by label; use parse_dimensions")
        for key in ("max_series", "max_spans", "max_input_bytes", "max_line_bytes"):
            integer(getattr(self, key), key, 1, 2**63 - 1)
        if self.input_format not in ("json", "jsonl"):
            raise InputError("input_format must be json or jsonl")

    @property
    def series_per_group(self) -> int:
        return len(self.buckets) + 5  # calls, errors, finite buckets, +Inf, sum, count


@dataclass
class Group:
    count: int
    errors: int
    sum_ns: int
    buckets: list[int]


@dataclass
class Result:
    report: dict
    prometheus: str | None


def canonical_json(value: Any) -> str:
    # Decimal JSON numbers stay tagged so no precision is lost in duplicate comparisons.
    if isinstance(value, Decimal):
        return "decimal:" + str(value)
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + ":" + canonical_json(v) for k, v in sorted(value.items())) + "}"
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=True)


def validate_span(span: Any, resource_attrs: dict, config: Config, where: str) -> tuple:
    span = obj(span, where, {"traceId", "spanId", "parentSpanId", "traceState", "flags", "name", "kind",
                            "startTimeUnixNano", "endTimeUnixNano", "attributes", "droppedAttributesCount",
                            "events", "droppedEventsCount", "links", "droppedLinksCount", "status"})
    trace_id = ident(span.get("traceId"), where + ".traceId", 32)
    span_id = ident(span.get("spanId"), where + ".spanId", 16)
    if "parentSpanId" in span:
        ident(span["parentSpanId"], where + ".parentSpanId", 16, optional=True)
    for key in ("traceState",):
        if key in span:
            string(span[key], where + "." + key)
    name = string(span.get("name"), where + ".name", nonempty=True)
    kind = integer(span.get("kind", 0), where + ".kind", 0, 5)
    start = integer(span.get("startTimeUnixNano"), where + ".startTimeUnixNano", 0, UINT64_MAX, decimal_string=True)
    end = integer(span.get("endTimeUnixNano"), where + ".endTimeUnixNano", 0, UINT64_MAX, decimal_string=True)
    if end < start:
        raise InputError(f"{where}: end timestamp precedes start")
    attrs = attributes(span.get("attributes", []), where + ".attributes")
    for key in ("flags", "droppedAttributesCount", "droppedEventsCount", "droppedLinksCount"):
        integer(span.get(key, 0), where + "." + key, 0, 2**32 - 1)
    status = obj(span.get("status", {}), where + ".status", {"code", "message"})
    code = integer(status.get("code", 0), where + ".status.code", 0, 2)
    if "message" in status:
        string(status["message"], where + ".status.message")
    for i, event in enumerate(array(span.get("events", []), where + ".events")):
        path = f"{where}.events[{i}]"
        event = obj(event, path, {"timeUnixNano", "name", "attributes", "droppedAttributesCount"})
        integer(event.get("timeUnixNano", 0), path + ".timeUnixNano", 0, UINT64_MAX, decimal_string=True)
        string(event.get("name", ""), path + ".name")
        attributes(event.get("attributes", []), path + ".attributes")
        integer(event.get("droppedAttributesCount", 0), path + ".droppedAttributesCount", 0, 2**32 - 1)
    for i, link in enumerate(array(span.get("links", []), where + ".links")):
        path = f"{where}.links[{i}]"
        link = obj(link, path, {"traceId", "spanId", "traceState", "attributes", "droppedAttributesCount", "flags"})
        ident(link.get("traceId"), path + ".traceId", 32)
        ident(link.get("spanId"), path + ".spanId", 16)
        string(link.get("traceState", ""), path + ".traceState")
        attributes(link.get("attributes", []), path + ".attributes")
        for key in ("flags", "droppedAttributesCount"):
            integer(link.get(key, 0), path + "." + key, 0, 2**32 - 1)
    service = resource_attrs.get("service.name")
    if service is None or service[0] != "stringValue":
        raise InputError(f"{where}: resource service.name must be a nonempty stringValue")
    service_name = string(service[1], where + ".service.name", nonempty=True)
    labels = [service_name, name, str(kind), str(code)]
    for dim in config.dimensions:
        source = resource_attrs if dim.source == "resource" else attrs
        labels.append(dimension_value(source.get(dim.key), f"{where}.{dim.source}:{dim.key}"))
    return (trace_id, span_id), tuple(labels), end - start, code == 2, attrs


def escape_label(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def exposition(groups: dict, config: Config) -> str:
    names = [d["label"] for d in BASE_DIMENSIONS] + [d.label for d in config.dimensions]
    calls, errors, durations = [], [], []
    # Prometheus text format requires each metric's samples to form one contiguous group
    # following its HELP/TYPE lines, so each metric is collected separately.
    for values, group in sorted(groups.items()):
        labels = ",".join(f'{name}="{escape_label(value)}"' for name, value in zip(names, values))
        calls.append(f"spanmetrics_calls_total{{{labels}}} {group.count}")
        errors.append(f"spanmetrics_errors_total{{{labels}}} {group.errors}")
        for bound, count in zip(config.buckets, group.buckets):
            durations.append(f'spanmetrics_duration_seconds_bucket{{{labels},le="{decimal_text(bound)}"}} {count}')
        durations.extend([f'spanmetrics_duration_seconds_bucket{{{labels},le="+Inf"}} {group.count}',
                          f"spanmetrics_duration_seconds_count{{{labels}}} {group.count}",
                          f"spanmetrics_duration_seconds_sum{{{labels}}} {seconds(group.sum_ns)}"])
    lines = ["# HELP spanmetrics_calls_total Unique captured spans.", "# TYPE spanmetrics_calls_total counter", *calls,
             "# HELP spanmetrics_errors_total Unique captured spans with status code 2.", "# TYPE spanmetrics_errors_total counter", *errors,
             "# HELP spanmetrics_duration_seconds Captured span duration in seconds.", "# TYPE spanmetrics_duration_seconds histogram", *durations]
    return "\n".join(lines) + "\n"


def empty_report(config: Config) -> dict:
    return {"schema_version": 1, "tool_version": __version__,
            "input_format": "OTLP/JSONL ExportTraceServiceRequest per line" if config.input_format == "jsonl" else
                            "OTLP/JSON ExportTraceServiceRequest",
            "status": "ok", "input_records": 0, "unique_records": 0,
            "duplicate_records": 0, "rejected_records": 0, "errors": 0, "groups": 0, "series": 0,
            "duration_sum_seconds": "0", "dimensions": list(BASE_DIMENSIONS) + [vars(d) for d in config.dimensions],
            "custom_dimension_encoding": "missing | s:<string> | b:<boolean> | i:<integer> | d:<decimal>",
            "histogram_bounds_seconds": [decimal_text(b) for b in config.buckets],
            "limits": {k: getattr(config, k) for k in ("max_series", "max_spans", "max_input_bytes", "max_line_bytes")},
            "duplicate_policy": "Same normalized ID pair and identical full JSON span/resource/scope context. "
                                "Decoded object key order is ignored; array order, JSON value types and "
                                "explicit versus omitted fields are significant.",
            "diagnostics": [], "diagnostics_omitted": 0, "losses": list(METRIC_LOSSES),
            "omitted_attribute_keys": {"resource": [], "span": [], "scope": []},
            "omitted_fields": {}, "metrics_emitted": False}


def diagnostic(report: dict, message: str) -> None:
    report["status"] = "rejected"
    if len(report["diagnostics"]) < 100:
        report["diagnostics"].append(message)
    else:
        report["diagnostics_omitted"] += 1


def strict_loads(data: bytes, *, context: str = "OTLP JSON") -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InputError(f"duplicate JSON object key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise InputError(f"non-JSON numeric constant: {value}")

    def number(value):
        # Bound the representation before Decimal formatting can expand an exponent.
        if len(value) > 128:
            raise InputError("JSON fractional number exceeds 128 characters")
        exponent = re.search(r"[eE]([+-]?[0-9]+)$", value)
        if exponent and (len(exponent[1]) > 5 or abs(int(exponent[1])) > 400):
            raise InputError("JSON fractional number exponent must be within -400..400")
        return Decimal(value)

    try:
        return json.loads(data.decode("utf-8"), parse_float=number, parse_constant=constant, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError, InvalidOperation) as exc:
        raise InputError(f"invalid {context}: {exc}") from exc


def convert(data: bytes, config: Config | None = None) -> Result:
    return _convert(data, config or Config())


def requests(data: bytes, config: Config):
    """Decode one bounded request at a time, without splitting the whole capture."""
    if config.input_format == "json":
        yield strict_loads(data), "$"
        return
    stream = io.BytesIO(data)
    line_number = 0
    while True:
        # One over the payload cap plus room for CRLF. No unbounded line allocation.
        line = stream.readline(min(config.max_line_bytes + 3, len(data) + 1))
        if not line:
            break
        line_number += 1
        where = f"line {line_number}:$"
        if line.endswith(b"\n"):
            line = line[:-1]
            if line.endswith(b"\r"):
                line = line[:-1]
        if len(line) > config.max_line_bytes:
            raise InputError(f"{where}: line byte cap {config.max_line_bytes} exceeded")
        try:
            root = strict_loads(line)
        except InputError as exc:
            raise InputError(f"{where}: {exc}") from exc
        yield root, where
    if line_number == 0:
        raise InputError("empty JSONL capture; expected at least one ExportTraceServiceRequest")


def request_records(root: Any, where: str, remaining_spans: int, max_spans: int) -> list:
    """Validate each request's containers before classifying any of its records."""
    root = obj(root, where, {"resourceSpans"})
    resources = array(root.get("resourceSpans"), where + ".resourceSpans")
    records = []
    for ri, rs in enumerate(resources):
        rp = f"{where}.resourceSpans[{ri}]"
        rs = obj(rs, rp, {"resource", "scopeSpans", "schemaUrl"})
        schema(rs, rp)
        resource_attrs = metadata(rs.get("resource", {}), rp + ".resource")
        resource_context = {k: rs[k] for k in ("resource", "schemaUrl") if k in rs}
        resource_hash = hashlib.sha256(canonical_json(resource_context).encode()).digest()
        for si, ss in enumerate(array(rs.get("scopeSpans", []), rp + ".scopeSpans")):
            sp = f"{rp}.scopeSpans[{si}]"
            ss = obj(ss, sp, {"scope", "spans", "schemaUrl"})
            schema(ss, sp)
            scope_attrs = metadata(ss.get("scope", {}), sp + ".scope", scope=True)
            scope_context = {k: ss[k] for k in ("scope", "schemaUrl") if k in ss}
            context_hash = hashlib.sha256(resource_hash + b"\0" + canonical_json(scope_context).encode()).digest()
            for i, span in enumerate(array(ss.get("spans", []), sp + ".spans")):
                if len(records) >= remaining_spans:
                    raise InputError(f"{where}: input exceeds span cap {max_spans}; no partial conversion")
                records.append((span, f"{sp}.spans[{i}]", resource_attrs, scope_attrs, context_hash))
    return records


def _convert(data: bytes, config: Config, accepted_labels: dict | None = None) -> Result:
    # Comparison observes only accepted unique records, using the same validation,
    # duplicate policy and series cap as normal metric generation.
    report = empty_report(config)
    report["input_sha256"] = hashlib.sha256(data).hexdigest()
    groups = {}
    seen = {}
    omitted_attrs = {"resource": set(), "span": set(), "scope": set()}
    omitted_fields = Counter()
    bounds = [Fraction(b) * 1_000_000_000 for b in config.buckets]
    try:
        if len(data) > config.max_input_bytes:
            raise InputError(f"input exceeds byte cap {config.max_input_bytes}")
        for root, request_path in requests(data, config):
            records = request_records(root, request_path, config.max_spans - report["input_records"], config.max_spans)
            report["input_records"] += len(records)
            for span, where, resource_attrs, scope_attrs, context in records:
                try:
                    identity, labels, ns, is_error, attrs = validate_span(span, resource_attrs, config, where)
                    fingerprint = hashlib.sha256(context + b"\0" + canonical_json(span).encode()).digest()
                    if identity in seen:
                        if seen[identity] != fingerprint:
                            raise InputError(f"{where}: conflicting duplicate traceId={identity[0]} spanId={identity[1]}")
                        report["duplicate_records"] += 1
                        continue
                    if labels not in groups:
                        if (len(groups) + 1) * config.series_per_group > config.max_series:
                            raise InputError(f"{where}: series cap {config.max_series} exceeded; "
                                             f"each group requires {config.series_per_group} series")
                        groups[labels] = Group(0, 0, 0, [0] * len(bounds))
                    seen[identity] = fingerprint
                    if accepted_labels is not None:
                        accepted_labels[identity] = labels
                    group = groups[labels]
                    group.count += 1
                    group.errors += int(is_error)
                    group.sum_ns += ns
                    for i, bound_ns in enumerate(bounds):
                        group.buckets[i] += int(ns <= bound_ns)
                    report["unique_records"] += 1
                    report["errors"] += int(is_error)
                    for source, mapping in (("resource", resource_attrs), ("span", attrs), ("scope", scope_attrs)):
                        allowed = {d.key for d in config.dimensions if d.source == source}
                        if source == "resource":
                            allowed.add("service.name")
                        omitted_attrs[source].update(set(mapping) - allowed)
                    for key in ("parentSpanId", "traceState", "flags", "events", "links", "droppedAttributesCount",
                                "droppedEventsCount", "droppedLinksCount"):
                        if span.get(key):
                            omitted_fields[key] += len(span[key]) if key in ("events", "links") else 1
                    if span.get("status", {}).get("message"):
                        omitted_fields["status.message"] += 1
                except InputError as exc:
                    report["rejected_records"] += 1
                    diagnostic(report, str(exc))
    except (InputError, RecursionError) as exc:
        report["record_counts_complete"] = False
        diagnostic(report, str(exc))
    else:
        report["record_counts_complete"] = True
    report["groups"] = len(groups)
    report["series"] = len(groups) * config.series_per_group
    report["duration_sum_seconds"] = seconds(sum(g.sum_ns for g in groups.values()))
    report["omitted_attribute_keys"] = {k: sorted(v) for k, v in omitted_attrs.items()}
    report["omitted_fields"] = dict(sorted(omitted_fields.items()))
    report["totals_scope"] = "validated unique records; no metrics emitted if any rejection"
    prom = exposition(groups, config) if report["status"] == "ok" else None
    if prom is not None:
        report["prometheus_sha256"] = hashlib.sha256(prom.encode()).hexdigest()
    return Result(report, prom)
