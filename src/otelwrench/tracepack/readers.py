# SPDX-License-Identifier: Apache-2.0
"""Readers for OTLP JSON, Jaeger UI exports and Zipkin v2 JSON."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .jsonfmt import UNDEFINED, DecimalToken, JSBigInt, js_keys, js_quote
from .model import (
    I64,
    U64,
    LossCollector,
    array,
    base_span,
    boolean,
    fail,
    field,
    get,
    id_,
    integer,
    object_,
    put,
    string,
    typed,
)


def _opt_array(x: Any, path: str) -> list[Any]:
    return [] if x is UNDEFINED else array(x, path)


def _time(x: Any, path: str, nanos: bool = False) -> int:
    return integer(x, path, nanos=nanos, max=U64 if nanos else U64 // 1000) * (1 if nanos else 1000)


def _int_attr(x: Any, path: str) -> int:
    return integer(x, path, min=-I64 - 1, max=I64)


def _number(x: Any, path: str) -> float:
    if isinstance(x, DecimalToken):
        x = x.number
    elif type(x) is JSBigInt:
        try:
            x = float(x)
        except OverflowError:
            x = math.inf if x > 0 else -math.inf
    if type(x) not in (int, float) or not math.isfinite(x):
        fail(path, "expected finite number")
    return float(x)


def _index(options: list[str], value: Any) -> int:
    if isinstance(value, str) and value in options:
        return options.index(value)
    return -1


def _count_loss(x: Mapping[str, Any], key: str, path: str, loss: LossCollector) -> None:
    value = get(x, key)
    if value is not UNDEFINED and integer(value, f"{path}.{key}", max=4294967295) != 0:
        loss.add(f"{path}.{key}", "source reports previously dropped data")


def _without_dropped(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v["type"] != "dropped"}


def _otlp_attrs(source: Any, path: str, loss: LossCollector) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for i, a in enumerate(_opt_array(source, path)):
        ap = f"{path}[{i}]"
        object_(a, ap)
        loss.unknown(a, ["key", "value"], ap)
        key = string(get(a, "key"), f"{ap}.key")
        value = object_(get(a, "value"), f"{ap}.value")
        keys = js_keys(value)
        if len(keys) != 1:
            fail(f"{ap}.value", "expected exactly one AnyValue variant")
        k = keys[0]
        v = value[k]
        vp = f"{ap}.value.{k}"
        if k == "stringValue":
            t = typed("string", string(v, vp), ap)
        elif k == "boolValue":
            t = typed("bool", boolean(v, vp), ap)
        elif k == "intValue":
            t = typed("int64", _int_attr(v, vp), ap)
        elif k == "doubleValue":
            t = typed("float64", _number(v, vp), ap)
        elif k in ("arrayValue", "kvlistValue", "bytesValue"):
            loss.add(vp, "nonprimitive attribute dropped")
            if key in out:
                fail(ap, f"duplicate attribute key {js_quote(key)}")
            # Track even dropped keys so duplicate attributes cannot silently overwrite.
            put(out, key, typed("dropped", None, ap), ap)
            continue
        else:
            fail(vp, "unsupported AnyValue variant")
        put(out, key, t, ap)
    return _without_dropped(out)


def _jaeger_attrs(source: Any, path: str, loss: LossCollector) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for i, a in enumerate(_opt_array(source, path)):
        ap = f"{path}[{i}]"
        object_(a, ap)
        loss.unknown(a, ["key", "type", "value"], ap)
        key = string(get(a, "key"), f"{ap}.key")
        t = string(get(a, "type"), f"{ap}.type")
        raw = get(a, "value")
        if t == "string":
            v = string(raw, f"{ap}.value")
        elif t == "bool":
            v = boolean(raw, f"{ap}.value")
        elif t == "int64":
            v = _int_attr(raw, f"{ap}.value")
        elif t == "float64":
            v = _number(raw, f"{ap}.value")
        elif t == "binary":
            string(raw, f"{ap}.value")
            loss.add(ap, "binary attribute dropped")
            put(out, key, typed("dropped", None, ap), ap)
            continue
        else:
            fail(f"{ap}.type", f"unsupported Jaeger tag type {t}")
        put(out, key, typed(t, v, ap), ap)
    return _without_dropped(out)


def _service(resource: dict[str, Any], value: Any, path: str) -> None:
    string(value, path)
    if "service.name" in resource:
        fail(path, "service.name conflicts with service field")
    resource["service.name"] = typed("string", value, path)


def _error_index(items: Any) -> int:
    if not isinstance(items, list):
        return -1
    for i, a in enumerate(items):
        if isinstance(a, Mapping) and isinstance(a.get("key"), str) and a.get("key") == "error":
            return i
    return -1


_OTLP_SPAN_KEYS = ["traceId", "spanId", "parentSpanId", "name", "startTimeUnixNano", "endTimeUnixNano", "kind",
                   "status", "attributes", "events", "droppedAttributesCount", "droppedEventsCount",
                   "droppedLinksCount"]


def read_otlp(source: Any, loss: LossCollector) -> list[dict[str, Any]]:
    object_(source, "$")
    loss.unknown(source, ["resourceSpans"], "$")
    spans: list[dict[str, Any]] = []
    for r, rs in enumerate(array(get(source, "resourceSpans"), "$.resourceSpans")):
        rp = f"$.resourceSpans[{r}]"
        object_(rs, rp)
        loss.unknown(rs, ["resource", "scopeSpans"], rp)
        resource = {} if get(rs, "resource") is UNDEFINED else object_(rs["resource"], f"{rp}.resource")
        loss.unknown(resource, ["attributes", "droppedAttributesCount"], f"{rp}.resource")
        _count_loss(resource, "droppedAttributesCount", f"{rp}.resource", loss)
        attrs = _otlp_attrs(get(resource, "attributes"), f"{rp}.resource.attributes", loss)
        service = attrs.get("service.name")
        if service is not None and service["type"] != "string":
            fail(service["path"], "service.name must be a string")
        groups = _opt_array(get(rs, "scopeSpans"), f"{rp}.scopeSpans")
        if not any(isinstance(g, Mapping) and isinstance(g.get("spans"), list) and g["spans"] for g in groups) and len(rs):
            loss.add(rp, "empty resource group metadata/container dropped")
        for g, ss in enumerate(groups):
            gp = f"{rp}.scopeSpans[{g}]"
            object_(ss, gp)
            loss.unknown(ss, ["spans"], gp)
            for i, s in enumerate(_opt_array(get(ss, "spans"), f"{gp}.spans")):
                p = f"{gp}.spans[{i}]"
                object_(s, p)
                loss.unknown(s, _OTLP_SPAN_KEYS, p)
                for key in ("droppedAttributesCount", "droppedEventsCount", "droppedLinksCount"):
                    _count_loss(s, key, p, loss)
                paths: dict[str, str] = {"start": f"{p}.startTimeUnixNano", "end": f"{p}.endTimeUnixNano",
                                         "kind": f"{p}.kind"}
                trace_id = id_(get(s, "traceId"), f"{p}.traceId", True)
                span_id = id_(get(s, "spanId"), f"{p}.spanId")
                parent = get(s, "parentSpanId")
                parent_id = "" if parent is UNDEFINED or (isinstance(parent, str) and parent == "") \
                    else id_(parent, f"{p}.parentSpanId")
                name = string(get(s, "name"), f"{p}.name")
                start = _time(get(s, "startTimeUnixNano"), paths["start"], True)
                end = _time(get(s, "endTimeUnixNano"), paths["end"], True)
                kind = 0 if get(s, "kind") is UNDEFINED else integer(s["kind"], f"{p}.kind", max=5)
                span_attrs = _otlp_attrs(get(s, "attributes"), f"{p}.attributes", loss)
                span = base_span(path=p, paths=paths, resource=dict(attrs), traceId=trace_id, spanId=span_id,
                                 parentId=parent_id, name=name, start=start, end=end, kind=kind, attrs=span_attrs)
                # Keep presence/path even when a nonprimitive error attribute was dropped.
                error_index = _error_index(get(s, "attributes"))
                if error_index != -1:
                    paths["errorAttribute"] = f"{p}.attributes[{error_index}]"
                status = get(s, "status")
                if status is not UNDEFINED:
                    object_(status, f"{p}.status")
                    loss.unknown(status, ["code", "message"], f"{p}.status")
                    code = get(status, "code")
                    message = get(status, "message")
                    span["status"] = {
                        "code": 0 if code is UNDEFINED else integer(code, f"{p}.status.code", max=2),
                        "message": "" if message is UNDEFINED else string(message, f"{p}.status.message"),
                    }
                    if code is not UNDEFINED:
                        paths["statusCode"] = f"{p}.status.code"
                    if message is not UNDEFINED:
                        paths["statusMessage"] = f"{p}.status.message"
                events = []
                for j, e in enumerate(_opt_array(get(s, "events"), f"{p}.events")):
                    ep = f"{p}.events[{j}]"
                    object_(e, ep)
                    loss.unknown(e, ["timeUnixNano", "name", "attributes", "droppedAttributesCount"], ep)
                    _count_loss(e, "droppedAttributesCount", ep, loss)
                    time = _time(get(e, "timeUnixNano"), f"{ep}.timeUnixNano", True)
                    event_name = string(get(e, "name"), f"{ep}.name")
                    events.append({"path": ep, "timePath": f"{ep}.timeUnixNano", "time": time, "name": event_name,
                                   "attrs": _otlp_attrs(get(e, "attributes"), f"{ep}.attributes", loss)})
                span["events"] = events
                spans.append(span)
    return spans


KINDS = ["unspecified", "internal", "server", "client", "producer", "consumer"]


def _jaeger_conventions(span: dict[str, Any]) -> None:
    attrs = span["attrs"]

    def take(key: str, type_: str) -> dict[str, Any] | None:
        v = attrs.get(key)
        if not v:
            return None
        if v["type"] != type_:
            fail(v["path"], f"{key} must be {type_}")
        del attrs[key]
        return v

    code = take("otel.status_code", "string")
    if code:
        i = _index(["UNSET", "OK", "ERROR"], code["value"])
        if i == -1:
            fail(code["path"], "unsupported otel.status_code")
        span["status"]["code"] = i
        span["paths"]["statusCode"] = code["path"]
    elif attrs.get("error") and attrs["error"]["type"] == "bool" and attrs["error"]["value"]:
        span["status"]["code"] = 2
        span["paths"]["statusCode"] = attrs["error"]["path"]
    desc = take("otel.status_description", "string")
    if desc:
        span["status"]["message"] = desc["value"]
        span["paths"]["statusMessage"] = desc["path"]
    kind = take("span.kind", "string")
    if kind:
        i = _index(KINDS, kind["value"])
        if i == -1:
            fail(kind["path"], "unsupported span.kind")
        span["kind"] = i
        span["paths"]["kind"] = kind["path"]


_JAEGER_SPAN_KEYS = ["traceID", "spanID", "operationName", "references", "startTime", "duration", "tags", "logs",
                     "processID"]


def read_jaeger(source: Any, loss: LossCollector) -> list[dict[str, Any]]:
    object_(source, "$")
    loss.unknown(source, ["data", "total", "limit", "offset", "errors"], "$")
    # Query envelope metadata is not trace data, but must still be auditable.
    for k in ("total", "limit", "offset", "errors"):
        value = get(source, k)
        if value is not UNDEFINED and value is not None:
            loss.add(f"$.{k}", "Jaeger query metadata dropped")
    spans: list[dict[str, Any]] = []
    for t, trace in enumerate(array(get(source, "data"), "$.data")):
        tp = f"$.data[{t}]"
        object_(trace, tp)
        loss.unknown(trace, ["traceID", "spans", "processes"], tp)
        tid = id_(get(trace, "traceID"), f"{tp}.traceID", True, True)
        processes = object_(get(trace, "processes"), f"{tp}.processes")
        resources: dict[str, dict[str, Any]] = {}
        used: set[str] = set()
        for key in js_keys(processes):
            proc = processes[key]
            pp = field(f"{tp}.processes", key)
            object_(proc, pp)
            loss.unknown(proc, ["serviceName", "tags"], pp)
            resource = _jaeger_attrs(get(proc, "tags"), f"{pp}.tags", loss)
            _service(resource, get(proc, "serviceName"), f"{pp}.serviceName")
            resources[key] = resource
        items = array(get(trace, "spans"), f"{tp}.spans")
        if not items:
            loss.add(tp, "empty trace identity/container dropped")
        for i, s in enumerate(items):
            p = f"{tp}.spans[{i}]"
            object_(s, p)
            loss.unknown(s, _JAEGER_SPAN_KEYS, p)
            span_trace = id_(get(s, "traceID"), f"{p}.traceID", True, True)
            if span_trace != tid:
                fail(f"{p}.traceID", "span traceID does not match enclosing trace")
            pid = string(get(s, "processID"), f"{p}.processID")
            if pid not in resources:
                fail(f"{p}.processID", "unknown process")
            used.add(pid)
            paths: dict[str, str] = {"start": f"{p}.startTime", "end": f"{p}.duration"}
            start = _time(get(s, "startTime"), paths["start"])
            duration = _time(get(s, "duration"), paths["end"])
            span_id = id_(get(s, "spanID"), f"{p}.spanID")
            name = string(get(s, "operationName"), f"{p}.operationName")
            span = base_span(path=p, paths=paths, traceId=tid, spanId=span_id, name=name, start=start,
                             end=start + duration, resource=dict(resources[pid]),
                             attrs=_jaeger_attrs(get(s, "tags"), f"{p}.tags", loss))
            # Keep presence/path even when a binary error tag was dropped.
            error_index = _error_index(get(s, "tags"))
            if error_index != -1:
                paths["errorAttribute"] = f"{p}.tags[{error_index}]"
            refs = _opt_array(get(s, "references"), f"{p}.references")
            if len(refs) > 1:
                fail(f"{p}.references", "multiple Jaeger references unsupported")
            if refs:
                ref = object_(refs[0], f"{p}.references[0]")
                loss.unknown(ref, ["refType", "traceID", "spanID"], f"{p}.references[0]")
                ref_type = get(ref, "refType")
                if not (isinstance(ref_type, str) and ref_type == "CHILD_OF"):
                    fail(f"{p}.references[0].refType", "only CHILD_OF supported")
                if id_(get(ref, "traceID"), f"{p}.references[0].traceID", True, True) != tid:
                    fail(f"{p}.references[0].traceID", "cross-trace reference unsupported")
                span["parentId"] = id_(get(ref, "spanID"), f"{p}.references[0].spanID")
            _jaeger_conventions(span)
            events = []
            for j, e in enumerate(_opt_array(get(s, "logs"), f"{p}.logs")):
                ep = f"{p}.logs[{j}]"
                object_(e, ep)
                loss.unknown(e, ["timestamp", "fields"], ep)
                attrs = _jaeger_attrs(get(e, "fields"), f"{ep}.fields", loss)
                event = attrs.get("event")
                if event and event["type"] != "string":
                    fail(event["path"], "event name field must be string")
                attrs.pop("event", None)
                time = _time(get(e, "timestamp"), f"{ep}.timestamp")
                events.append({"path": ep, "timePath": f"{ep}.timestamp", "time": time,
                               "name": event["value"] if event else "", "attrs": attrs})
            span["events"] = events
            spans.append(span)
        for key in resources:
            if key not in used:
                loss.add(field(f"{tp}.processes", key), "unreferenced process dropped")
    return spans


_ZIPKIN_SPAN_KEYS = ["traceId", "id", "parentId", "name", "timestamp", "duration", "localEndpoint", "tags",
                     "annotations", "kind", "shared"]
ZIPKIN_KINDS = ["SERVER", "CLIENT", "PRODUCER", "CONSUMER"]


def read_zipkin(source: Any, loss: LossCollector) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for i, s in enumerate(array(source, "$")):
        p = f"$[{i}]"
        object_(s, p)
        loss.unknown(s, _ZIPKIN_SPAN_KEYS, p)
        shared = get(s, "shared")
        if shared is not UNDEFINED and boolean(shared, f"{p}.shared"):
            fail(f"{p}.shared", "Zipkin shared spans unsupported")
        paths: dict[str, str] = {"start": f"{p}.timestamp", "end": f"{p}.duration"}
        start = _time(get(s, "timestamp"), paths["start"])
        duration = _time(get(s, "duration"), paths["end"])
        trace_id = id_(get(s, "traceId"), f"{p}.traceId", True, True)
        span_id = id_(get(s, "id"), f"{p}.id")
        parent = get(s, "parentId")
        parent_id = "" if parent is UNDEFINED else id_(parent, f"{p}.parentId")
        name = string(get(s, "name"), f"{p}.name")
        span = base_span(path=p, paths=paths, traceId=trace_id, spanId=span_id, parentId=parent_id, name=name,
                         start=start, end=start + duration)
        endpoint = get(s, "localEndpoint")
        if endpoint is not UNDEFINED:
            object_(endpoint, f"{p}.localEndpoint")
            loss.unknown(endpoint, ["serviceName"], f"{p}.localEndpoint")
            if get(endpoint, "serviceName") is not UNDEFINED:
                _service(span["resource"], endpoint["serviceName"], f"{p}.localEndpoint.serviceName")
        kind = get(s, "kind")
        if kind is not UNDEFINED:
            k = _index(ZIPKIN_KINDS, kind)
            if k == -1:
                fail(f"{p}.kind", "unsupported Zipkin kind")
            span["kind"] = k + 2
        tags = get(s, "tags")
        if tags is not UNDEFINED:
            object_(tags, f"{p}.tags")
            for key in js_keys(tags):
                tag_path = field(f"{p}.tags", key)
                span["attrs"][key] = typed("string", string(tags[key], tag_path), tag_path)
            if "error" in span["attrs"]:
                span["status"]["code"] = 2
        events = []
        for j, e in enumerate(_opt_array(get(s, "annotations"), f"{p}.annotations")):
            ep = f"{p}.annotations[{j}]"
            object_(e, ep)
            loss.unknown(e, ["timestamp", "value"], ep)
            time = _time(get(e, "timestamp"), f"{ep}.timestamp")
            events.append({"path": ep, "timePath": f"{ep}.timestamp", "time": time,
                           "name": string(get(e, "value"), f"{ep}.value"), "attrs": {}})
        span["events"] = events
        spans.append(span)
    return spans
