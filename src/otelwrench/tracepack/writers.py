# SPDX-License-Identifier: Apache-2.0
"""Writers for OTLP JSON, Jaeger UI export JSON and Zipkin v2 JSON."""
from __future__ import annotations

from typing import Any

from .jsonfmt import stringify, u16
from .model import U64, LossCollector, fail, sorted_attrs
from .readers import KINDS, ZIPKIN_KINDS

_OTLP_VALUE = {"string": "stringValue", "bool": "boolValue", "int64": "intValue", "float64": "doubleValue"}
_STATUS = ["UNSET", "OK", "ERROR"]


def otlp_attrs(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": key, "value": {_OTLP_VALUE[v["type"]]: str(v["value"]) if v["type"] == "int64" else v["value"]}}
            for key, v in sorted_attrs(attrs)]


def write_otlp(spans: list[dict[str, Any]], loss: LossCollector | None = None, *,
               zipkin_error_tag: bool = False) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    for s in spans:
        resource = {"attributes": otlp_attrs(s["resource"])}
        key = stringify(resource)
        if key not in groups:
            groups[key] = {"resource": resource, "scopeSpans": [{"spans": []}]}
        out: dict[str, Any] = {
            "traceId": s["traceId"], "spanId": s["spanId"], "name": s["name"], "kind": s["kind"],
            "startTimeUnixNano": str(s["start"]), "endTimeUnixNano": str(s["end"]),
            "attributes": otlp_attrs(s["attrs"]),
            "events": [{"timeUnixNano": str(e["time"]), "name": e["name"], "attributes": otlp_attrs(e["attrs"])}
                       for e in s["events"]],
            "status": dict(s["status"]),
        }
        if s["parentId"]:
            out["parentSpanId"] = s["parentId"]
        groups[key]["scopeSpans"][0]["spans"].append(out)
    return {"resourceSpans": [v for _, v in sorted(groups.items(), key=lambda item: u16(item[0]))]}


def _jaeger_attrs(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": key, "type": v["type"], "value": v["value"]} for key, v in sorted_attrs(attrs)]


def _micro(n: int, path: str, loss: LossCollector) -> int:
    if n % 1000 != 0:
        loss.add(path, f"nanosecond precision truncated to microseconds (remainder {n % 1000} ns)")
    return n // 1000


def write_zipkin(spans: list[dict[str, Any]], loss: LossCollector, *, zipkin_error_tag: bool = False) -> list[Any]:
    """Zipkin v2's wire subset: string span tags, name-only annotations, four RPC/messaging
    kinds and positive integer microsecond durations."""
    output = []
    for s in spans:
        paths = s["paths"]
        tags: dict[str, str] = {}
        for key, v in sorted_attrs(s["attrs"]):
            if v["type"] == "string":
                tags[key] = v["value"]
            else:
                loss.add(v["path"], "nonstring span attribute dropped; Zipkin tags require strings")
        for key, v in sorted_attrs(s["resource"]):
            if key != "service.name":
                loss.add(v["path"], "resource attribute dropped; Zipkin only represents service.name")
        if s["kind"] == 1:
            loss.add(paths.get("kind"), "INTERNAL kind dropped; Zipkin has no corresponding kind")
        # A synthesized tag preserves ERROR but adds an attribute on re-import.
        if zipkin_error_tag and s["status"]["code"] == 2 and "error" not in tags:
            tags["error"] = ""
            loss.add(paths.get("statusCode"),
                     "string error tag synthesized from ERROR status; re-import adds an error attribute")
        # Our Zipkin reader infers ERROR from the presence of a string error tag.
        if s["status"]["code"] != (2 if "error" in tags else 0):
            anchor = paths.get("statusCode")
            if anchor is None:
                error = s["attrs"].get("error")
                anchor = error["path"] if error is not None else s["path"]
            loss.add(anchor, "status code cannot be preserved; Zipkin implies ERROR from an error tag and otherwise UNSET")
        if s["status"]["message"]:
            loss.add(paths.get("statusMessage"), "status message dropped; Zipkin has no separate status message")
        timestamp = _micro(s["start"], paths["start"], loss)
        nanos = s["end"] - s["start"]
        if nanos < 1000:
            duration = 1
            loss.add(paths["end"], "duration rounded up to Zipkin minimum of 1 microsecond")
            if (timestamp + duration) * 1000 > U64:
                fail(paths["end"], "Zipkin minimum duration would exceed uint64 nanoseconds on re-import")
        else:
            duration = _micro(nanos, paths["end"], loss)
        seen: set[str] = set()
        annotations = []
        for e in s["events"]:
            for _, v in sorted_attrs(e["attrs"]):
                loss.add(v["path"], "event attribute dropped; Zipkin annotations only represent time and name")
            annotation = {"timestamp": _micro(e["time"], e["timePath"], loss), "value": e["name"]}
            key = stringify(annotation)
            if key in seen:
                loss.add(e["path"], "duplicate annotation collapsed after Zipkin projection")
            else:
                seen.add(key)
                annotations.append(annotation)
        out: dict[str, Any] = {"traceId": s["traceId"], "id": s["spanId"], "name": s["name"], "timestamp": timestamp,
                               "duration": duration,
                               "tags": dict(sorted(tags.items(), key=lambda item: u16(item[0]))),
                               "annotations": annotations}
        if s["parentId"]:
            out["parentId"] = s["parentId"]
        if s["kind"] >= 2:
            out["kind"] = ZIPKIN_KINDS[s["kind"] - 2]
        service = s["resource"].get("service.name")
        if service:
            out["localEndpoint"] = {"serviceName": service["value"]}
        output.append(out)
    return output


def _process(s: dict[str, Any]) -> dict[str, Any]:
    service = s["resource"].get("service.name")
    return {"serviceName": service["value"] if service else "",
            "tags": _jaeger_attrs({k: v for k, v in s["resource"].items() if k != "service.name"})}


def write_jaeger(spans: list[dict[str, Any]], loss: LossCollector, *, zipkin_error_tag: bool = False) -> dict[str, Any]:
    traces: dict[str, list[dict[str, Any]]] = {}
    for s in spans:
        traces.setdefault(s["traceId"], []).append(s)
    data = []
    for trace_id, items in traces.items():
        process_keys: dict[str, dict[str, Any]] = {}
        for s in items:
            if not s["resource"].get("service.name"):
                loss.add(s["path"], "missing service.name represented as empty Jaeger serviceName")
            process = _process(s)
            process_keys[stringify(process)] = process
        ordered = sorted(process_keys.items(), key=lambda item: u16(item[0]))
        ids = {key: f"p{i + 1}" for i, (key, _) in enumerate(ordered)}
        processes = {ids[key]: p for key, p in ordered}
        output = []
        for s in items:
            for k in ("otel.status_code", "otel.status_description", "span.kind"):
                if k in s["attrs"]:
                    fail(s["attrs"][k]["path"], f"attribute collides with Jaeger convention {k}")
            tags = _jaeger_attrs(s["attrs"])
            tags.append({"key": "otel.status_code", "type": "string", "value": _STATUS[s["status"]["code"]]})
            if s["status"]["message"]:
                tags.append({"key": "otel.status_description", "type": "string", "value": s["status"]["message"]})
            if s["kind"]:
                tags.append({"key": "span.kind", "type": "string", "value": KINDS[s["kind"]]})
            tags.sort(key=lambda tag: u16(tag["key"]))
            start = _micro(s["start"], s["paths"]["start"], loss)
            duration = _micro(s["end"] - s["start"], s["paths"]["end"], loss)
            logs = []
            for e in s["events"]:
                if "event" in e["attrs"]:
                    fail(e["attrs"]["event"]["path"], "attribute collides with Jaeger log event name")
                fields = sorted([*_jaeger_attrs(e["attrs"]), {"key": "event", "type": "string", "value": e["name"]}],
                                key=lambda f: u16(f["key"]))
                logs.append({"timestamp": _micro(e["time"], e["timePath"], loss), "fields": fields})
            output.append({
                "traceID": trace_id, "spanID": s["spanId"], "operationName": s["name"],
                "references": [{"refType": "CHILD_OF", "traceID": trace_id, "spanID": s["parentId"]}]
                if s["parentId"] else [],
                "startTime": start, "duration": duration, "tags": tags, "logs": logs,
                "processID": ids[stringify(_process(s))],
            })
        data.append({"traceID": trace_id, "spans": output, "processes": processes})
    return {"data": data}
