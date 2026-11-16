# SPDX-License-Identifier: Apache-2.0
"""Strict OTLP JSON snapshot conversion. No network or upstream translator code."""
from __future__ import annotations

import json
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from prometheus_client.parser import text_string_to_metric_families

WITH = "UnderscoreEscapingWithSuffixes"
WITHOUT = "UnderscoreEscapingWithoutSuffixes"
STRATEGIES = (WITH, WITHOUT)
UNITS = {"": "", "s": "seconds", "ms": "milliseconds", "By": "bytes", "1": "ratio"}


class ConversionError(ValueError):
    def __init__(self, message: str, *, report: dict | None = None):
        super().__init__(message)
        self.report = report or {"status": "error", "errors": [message], "collisions": []}


def fail(path: str, message: str):
    raise ConversionError(f"{path}: {message}")


def load_json(text: str) -> dict:
    """Reject ambiguous keys and non-JSON numeric constants; retain decimal precision."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ConversionError(f"JSON: duplicate object key {key!r}")
            result[key] = value
        return result

    def constant(value):
        raise ConversionError(f"JSON: bare {value} is invalid; use OTLP quoted nonfinite doubles")

    try:
        result = json.loads(text, object_pairs_hook=pairs, parse_float=Decimal,
                            parse_constant=constant)
    except (ValueError, InvalidOperation, RecursionError) as exc:
        if isinstance(exc, ConversionError):
            raise
        raise ConversionError(f"JSON: {exc}") from exc
    return obj(result, "$", {"resourceMetrics"})


def obj(value: Any, path: str, allowed: set[str]) -> dict:
    if not isinstance(value, dict):
        fail(path, "expected an object")
    extra = value.keys() - allowed
    if extra:
        fail(path, f"unsupported fields: {', '.join(sorted(extra))}")
    return value


def array(value: Any, path: str) -> list:
    if not isinstance(value, list):
        fail(path, "expected an array")
    return value


def string(value: Any, path: str, *, nonempty=False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        fail(path, "expected a nonempty string" if nonempty else "expected a string")
    # Lone surrogates cannot be written as UTF-8 exposition.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        fail(path, "unpaired Unicode surrogate")
    return value


def integer(value: Any, path: str, low: int, high: int) -> int:
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"-?(0|[1-9][0-9]*)", value):
        try:
            number = int(value)
        except ValueError:
            fail(path, "integer is too long")
    else:
        fail(path, "expected a decimal integer or integer string")
    if not low <= number <= high:
        fail(path, f"integer outside [{low}, {high}]")
    return number


def double(value: Any, path: str, allow_nonfinite: bool) -> str:
    if isinstance(value, str):
        if value not in ("NaN", "Infinity", "-Infinity"):
            fail(path, "asDouble/doubleValue strings must be NaN, Infinity or -Infinity")
        number = Decimal(value)
    elif type(value) in (int, float, Decimal):
        number = Decimal(str(value))
    else:
        fail(path, "expected a JSON number or quoted OTLP nonfinite double")
    if not number.is_finite():
        if not allow_nonfinite:
            fail(path, "nonfinite double requires --allow-nonfinite")
        return "NaN" if number.is_nan() else ("+Inf" if number > 0 else "-Inf")
    if not math.isfinite(float(number)):
        fail(path, "double overflows IEEE-754 binary64")
    if number != 0 and float(number) == 0:
        fail(path, "double underflows IEEE-754 binary64")
    return str(number)


def scalar(value: Any, path: str, allow_nonfinite: bool) -> tuple[str, str]:
    value = obj(value, path, {"stringValue", "boolValue", "intValue", "doubleValue"})
    if len(value) != 1:
        fail(path, "AnyValue must contain exactly one supported scalar field")
    kind, raw = next(iter(value.items()))
    if kind == "stringValue":
        return string(raw, path), kind
    if kind == "boolValue":
        if type(raw) is not bool:
            fail(path, "boolValue must be a boolean")
        return str(raw).lower(), kind
    if kind == "intValue":
        return str(integer(raw, path, -(2**63), 2**63 - 1)), kind
    return double(raw, path, allow_nonfinite), kind


def attributes(value: Any, path: str, allow_nonfinite: bool) -> list[dict]:
    result, seen = [], set()
    for i, entry in enumerate(array(value, path)):
        p = f"{path}[{i}]"
        entry = obj(entry, p, {"key", "value"})
        key = string(entry.get("key"), p + ".key", nonempty=True)
        if key in seen:
            fail(p, f"duplicate attribute key {key!r}")
        seen.add(key)
        label_value, kind = scalar(entry.get("value"), p + ".value", allow_nonfinite)
        result.append({"key": key, "value": label_value, "value_type": kind, "path": p})
    return result


def milliseconds_to_seconds(value: str, path: str) -> str:
    """Shift the decimal exponent exactly, without ambient Decimal rounding."""
    number = Decimal(value)
    if not number.is_finite():
        # These have already passed the caller's explicit nonfinite policy.
        return value
    sign, digits, exponent = number.as_tuple()
    converted = Decimal((sign, digits, exponent - 3))
    # Validate the scaled result too: a valid input can now underflow binary64.
    return double(converted, path, allow_nonfinite=False)


def metric_name(name: str, unit: str, counter: bool, strategy: str, *,
                convert_milliseconds: bool = False) -> tuple[str, list[dict]]:
    steps = []
    base = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_")
    if not base:
        fail("metric.name", f"{name!r} has no ASCII letters or digits")
    if base != name:
        steps.append({"operation": "underscore_escape_and_trim", "before": name, "after": base})
    if base[0].isdigit():
        old, base = base, "_" + base
        steps.append({"operation": "digit_prefix", "before": old, "after": base})
    if convert_milliseconds:
        # A named unit must agree with scaled values, even without added suffixes.
        tail = "_total" if counter and base.endswith("_total") else ""
        stem = base[:-len(tail)] if tail else base
        if stem == "milliseconds" or stem.endswith("_milliseconds"):
            old, base = base, stem[:-len("milliseconds")] + "seconds" + tail
            steps.append({"operation": "converted_unit_name", "before": old, "after": base})
    if strategy == WITH:
        if counter and base.endswith("_total"):
            old, base = base, base[:-6]
            steps.append({"operation": "remove_existing_counter_suffix", "before": old, "after": base})
        suffix = UNITS[unit]
        if unit == "1" and counter:
            suffix = ""
        if suffix and not (base == suffix or base.endswith("_" + suffix)):
            old, base = base, base + "_" + suffix
            steps.append({"operation": "unit_suffix", "unit": unit, "before": old, "after": base})
        if counter:
            old, base = base, base + "_total"
            steps.append({"operation": "counter_suffix", "before": old, "after": base})
    return base, steps


def label_name(key: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", key)
    if name[0].isdigit():
        name = "key_" + name
    if name.startswith("__"):
        fail("attribute.key", f"{key!r} maps to reserved Prometheus label {name!r}")
    return name


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", r"\n").replace('"', r'\"')


def conversion_metric_names(names, convert_unit):
    """Canonicalize an optional exact-name selection; empty never means all."""
    if names is None:
        return None
    if convert_unit != "ms:s":
        raise ConversionError("convert_metrics requires unit conversion ms:s")
    if not isinstance(names, (list, tuple, set, frozenset)) or not names:
        raise ConversionError("convert_metrics: expected a nonempty collection of exact raw metric names")
    for name in names:
        string(name, "convert_metrics", nonempty=True)
    return sorted(set(names))


def convert(document: dict, *, strategy: str = WITH, promote_resource=(),
            allow_nonfinite: bool = False, convert_unit: str | None = None,
            convert_metrics=None) -> tuple[str, dict]:
    if strategy not in STRATEGIES:
        raise ConversionError(f"unsupported strategy {strategy!r}")
    if convert_unit is not None and convert_unit != "ms:s":
        raise ConversionError(f"unsupported unit conversion {convert_unit!r}; supported: ms:s")
    conversion_names = conversion_metric_names(convert_metrics, convert_unit)
    selection = set(conversion_names) if conversion_names is not None else None
    matched, wrong_units = set(), []
    promoted = set(promote_resource)
    for key in promoted:
        string(key, "promote_resource", nonempty=True)
    report = {"schema_version": 1, "status": "ok", "strategy": strategy,
              "unit_conversion": convert_unit,
              "resource_promotion": sorted(promoted), "metrics": [], "losses": [], "collisions": [],
              "numeric_policy": "Exact integer/decimal text. The Python parser retains integer literals; Prometheus uses binary64, so rounding may occur downstream.",
              "parser_convention": "Python parser removes a counter's terminal _total from family names and appends _total to sample names, including no-suffix input."}
    if conversion_names is not None:
        report["unit_conversion_metrics"] = conversion_names
    descriptors, targets, parser_targets, label_keys, series = {}, {}, {}, {}, {}
    families = {}

    def loss(path, reason, **details):
        report["losses"].append({"path": path, "reason": reason, **details})

    def collision(kind, target, first, second):
        item = {"kind": kind, "target": target, "first": first, "second": second}
        if item not in report["collisions"]:
            report["collisions"].append(item)

    def metadata(container, path, fields):
        for field in fields:
            if field not in container:
                continue
            value = container[field]
            p = path + "." + field
            if field in ("schemaUrl", "name", "version"):
                string(value, p)
            elif field == "droppedAttributesCount":
                # Upstream loss already happened; reporting it is essential.
                integer(value, p, 0, 2**32 - 1)
            elif field in ("startTimeUnixNano", "timeUnixNano"):
                integer(value, p, 0, 2**64 - 1)
            loss(p, "not represented in Prometheus snapshot", value=str(value))

    document = obj(document, "$", {"resourceMetrics"})
    for ri, resource_metrics in enumerate(array(document.get("resourceMetrics", []), "$.resourceMetrics")):
        rp = f"$.resourceMetrics[{ri}]"
        resource_metrics = obj(resource_metrics, rp, {"resource", "scopeMetrics", "schemaUrl"})
        metadata(resource_metrics, rp, ("schemaUrl",))
        resource = obj(resource_metrics.get("resource", {}), rp + ".resource", {"attributes", "droppedAttributesCount"})
        metadata(resource, rp + ".resource", ("droppedAttributesCount",))
        resource_attrs = attributes(resource.get("attributes", []), rp + ".resource.attributes", allow_nonfinite)
        selected = []
        for attr in resource_attrs:
            if attr["key"] in promoted:
                selected.append({**attr, "source": "resource"})
            else:
                loss(attr["path"], "resource attribute not in promotion allowlist", key=attr["key"])
        for si, scope_metrics in enumerate(array(resource_metrics.get("scopeMetrics", []), rp + ".scopeMetrics")):
            sp = f"{rp}.scopeMetrics[{si}]"
            scope_metrics = obj(scope_metrics, sp, {"scope", "metrics", "schemaUrl"})
            metadata(scope_metrics, sp, ("schemaUrl",))
            scope = obj(scope_metrics.get("scope", {}), sp + ".scope", {"name", "version", "attributes", "droppedAttributesCount"})
            metadata(scope, sp + ".scope", ("name", "version", "droppedAttributesCount"))
            for attr in attributes(scope.get("attributes", []), sp + ".scope.attributes", allow_nonfinite):
                loss(attr["path"], "scope attributes are not promoted", key=attr["key"])
            for mi, metric in enumerate(array(scope_metrics.get("metrics", []), sp + ".metrics")):
                mp = f"{sp}.metrics[{mi}]"
                metric = obj(metric, mp, {"name", "description", "unit", "gauge", "sum", "histogram", "exponentialHistogram", "summary", "metadata"})
                name = string(metric.get("name"), mp + ".name", nonempty=True)
                unit = string(metric.get("unit", ""), mp + ".unit")
                if unit not in UNITS:
                    fail(mp + ".unit", f"unsupported unit {unit!r}; supported: empty, s, ms, By, 1")
                description = string(metric.get("description", ""), mp + ".description")
                if "metadata" in metric:
                    fail(mp + ".metadata", "metric metadata is not supported")
                kinds = metric.keys() & {"gauge", "sum", "histogram", "exponentialHistogram", "summary"}
                if len(kinds) != 1:
                    fail(mp, "expected exactly one metric data type")
                kind = next(iter(kinds))
                if kind not in ("gauge", "sum"):
                    fail(mp, f"unsupported metric type {kind}")
                body = obj(metric[kind], mp + "." + kind, {"dataPoints"} if kind == "gauge" else {"dataPoints", "aggregationTemporality", "isMonotonic"})
                monotonic = body.get("isMonotonic", False)
                if kind == "sum":
                    if type(body.get("aggregationTemporality")) is not int or body["aggregationTemporality"] != 2:
                        fail(mp + ".sum.aggregationTemporality", "only cumulative numeric temporality 2 is supported; delta/unspecified rejected")
                    if type(monotonic) is not bool:
                        fail(mp + ".sum.isMonotonic", "expected a boolean")
                counter = kind == "sum" and monotonic
                target_type = "counter" if counter else "gauge"
                if selection is not None and name in selection:
                    matched.add(name)
                    if unit != "ms":
                        wrong_units.append((mp, name, unit))
                scale_ms = (convert_unit == "ms:s" and unit == "ms"
                            and (selection is None or name in selection))
                target_unit = "s" if scale_ms else unit
                target, steps = metric_name(name, target_unit, counter, strategy,
                                            convert_milliseconds=scale_ms)
                if scale_ms:
                    steps.insert(0, {"operation": "exact_unit_conversion", "before": unit,
                                     "after": target_unit, "factor": "0.001"})
                    loss(mp + ".unit", "unit conversion: original millisecond representation retained only in mapping; values emitted in seconds",
                         from_unit=unit, to_unit=target_unit, factor="0.001")
                parser_family = target[:-6] if counter and target.endswith("_total") else target
                parser_sample = parser_family + "_total" if counter else target
                descriptor = (kind, unit, description, monotonic)
                if name in descriptors and descriptors[name][0] != descriptor:
                    collision("conflicting_descriptor", name, descriptors[name][1], mp)
                else:
                    descriptors.setdefault(name, (descriptor, mp))
                for registry, key, ck in ((targets, target, "metric_name"), (parser_targets, parser_family, "parser_family")):
                    if key in registry and registry[key][0] != name:
                        collision(ck, key, registry[key][1], mp)
                    else:
                        registry.setdefault(key, (name, mp))
                entry = {"path": mp, "original_name": name, "original_type": kind, "original_unit": unit,
                         "target_unit": target_unit,
                         "original_description": description,
                         "is_monotonic": monotonic if kind == "sum" else None,
                         "aggregation_temporality": 2 if kind == "sum" else None,
                         "target_family": target, "target_type": target_type,
                         "parser_family": parser_family, "parser_sample": parser_sample,
                         "normalizations": steps, "data_points": []}
                report["metrics"].append(entry)
                if strategy == WITHOUT and unit:
                    loss(mp + ".unit", "unit retained only in mapping under no-suffix strategy", unit=target_unit)
                if kind == "sum" and not monotonic:
                    loss(mp + ".sum", "nonmonotonic cumulative sum represented as gauge; sum semantics are lost")
                family = families.setdefault(target, {"type": target_type, "description": description, "samples": []})
                points = array(body.get("dataPoints", []), mp + "." + kind + ".dataPoints")
                if not points:
                    loss(mp, "empty metric descriptor has no samples")
                for pi, point in enumerate(points):
                    pp = f"{mp}.{kind}.dataPoints[{pi}]"
                    point = obj(point, pp, {"attributes", "asInt", "asDouble", "startTimeUnixNano", "timeUnixNano", "flags", "exemplars"})
                    metadata(point, pp, ("startTimeUnixNano", "timeUnixNano"))
                    if "flags" in point:
                        if type(point["flags"]) is not int or point["flags"] != 0:
                            fail(pp + ".flags", "only flags=0 supported; no-recorded-value and unknown flags rejected")
                    if array(point.get("exemplars", []), pp + ".exemplars"):
                        fail(pp + ".exemplars", "nonempty exemplars are unsupported")
                    values = point.keys() & {"asInt", "asDouble"}
                    if len(values) != 1:
                        fail(pp, "expected exactly one asInt or asDouble")
                    value_kind = next(iter(values))
                    value = (str(integer(point[value_kind], pp + ".asInt", -(2**63), 2**63 - 1))
                             if value_kind == "asInt" else double(point[value_kind], pp + ".asDouble", allow_nonfinite))
                    if counter and value != "NaN" and Decimal(value) < 0:
                        fail(pp, "monotonic sum cannot have a negative value")
                    original_value = value
                    if scale_ms:
                        value = milliseconds_to_seconds(value, pp + "." + value_kind)
                        exact = Decimal(value)
                        if exact.is_finite() and Decimal.from_float(float(exact)) != exact:
                            loss(pp + "." + value_kind, "binary64 rounding: converted decimal is exact in text but not exactly representable by a binary64 backend",
                                 exact_value=value, unit=target_unit)
                    attrs = selected + [{**a, "source": "datapoint"} for a in attributes(point.get("attributes", []), pp + ".attributes", allow_nonfinite)]
                    labels, mappings, local = {}, [], {}
                    for attr in attrs:
                        key = attr["key"]
                        label = label_name(key)
                        if label in local:
                            collision("label_name", label, local[label], attr["path"])
                        local[label] = attr["path"]
                        previous = label_keys.get((target, label))
                        if previous and previous[0] != key:
                            collision("label_name", label, previous[1], attr["path"])
                        else:
                            label_keys.setdefault((target, label), (key, attr["path"]))
                        labels[label] = attr["value"]
                        if attr["value"] == "":
                            loss(attr["path"], "empty label value is equivalent to label absence in Prometheus", key=key)
                        operations = ([] if key == label else [{"operation": "label_underscore_escape_or_digit_prefix", "before": key, "after": label}])
                        if attr["value_type"] != "stringValue":
                            operations.append({"operation": "scalar_to_label_string", "from_type": attr["value_type"], "after": attr["value"]})
                        mappings.append({**attr, "target_label": label,
                                         "normalizations": operations})
                    identity = (parser_sample, tuple(sorted((k, v) for k, v in labels.items() if v != "")))
                    if identity in series:
                        collision("final_series", parser_sample, series[identity], pp)
                    else:
                        series[identity] = pp
                    entry["data_points"].append({"path": pp, "value_kind": value_kind, "value": value,
                                                 **({"original_value": original_value} if scale_ms else {}),
                                                 "labels": sorted(mappings, key=lambda a: (a["target_label"], a["source"], a["key"]))})
                    family["samples"].append((tuple(sorted(labels.items())), value))
    if report["collisions"]:
        report["status"] = "collision"
        raise ConversionError(f"{len(report['collisions'])} collision(s); no exposition emitted", report=report)
    if selection is not None:
        unmatched = sorted(selection - matched)
        if unmatched:
            raise ConversionError(f"convert_metrics: unmatched exact raw metric names: {unmatched!r}")
        if wrong_units:
            path, name, unit = wrong_units[0]
            fail(path + ".unit", f"selected metric {name!r} requires unit exactly 'ms'; got {unit!r}")
    lines = []
    for name, family in sorted(families.items()):
        if family["description"]:
            help_text = family["description"].replace("\\", "\\\\").replace("\n", r"\n")
            lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {family['type']}")
        for labels, value in sorted(family["samples"]):
            suffix = "{" + ",".join(f'{k}="{escape(v)}"' for k, v in labels) + "}" if labels else ""
            lines.append(f"{name}{suffix} {value}")
    text = "\n".join(lines) + ("\n" if lines else "")
    try:
        parsed = list(text_string_to_metric_families(text))
    except ValueError as exc:
        raise ConversionError(f"generated exposition failed prometheus_client parser: {exc}") from exc
    expected = {}
    for target, family in families.items():
        counter = family["type"] == "counter"
        pf = target[:-6] if counter and target.endswith("_total") else target
        ps = pf + "_total" if counter else target
        expected[pf] = (family["type"], {
            (ps, labels): (int(value) if re.fullmatch(r"-?[0-9]+", value) else float(value))
            for labels, value in family["samples"]
        })
    if len(parsed) != len(expected):
        raise ConversionError("parser family count mismatch")
    for family in parsed:
        if family.name not in expected or family.type != expected[family.name][0]:
            raise ConversionError("parser family name/type mismatch")
        expected_samples = expected[family.name][1]
        if len(family.samples) != len(expected_samples):
            raise ConversionError("parser sample count mismatch")
        for sample in family.samples:
            identity = (sample.name, tuple(sorted(sample.labels.items())))
            if identity not in expected_samples:
                raise ConversionError("parser sample name/labels mismatch")
            value = expected_samples[identity]
            if sample.value != value and not (math.isnan(sample.value) and math.isnan(value)):
                raise ConversionError("parser sample value mismatch")
    report["sample_count"] = len(series)
    report["validation"] = "prometheus_client.parser verified family names/types and sample names/labels/numeric values"
    return text, report
