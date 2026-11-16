# SPDX-License-Identifier: Apache-2.0
"""Normalize OTLP ``resourceSpans`` or Jaeger ``{data: [...]}`` captures into groups and edges."""
from __future__ import annotations

import math

from .jsontext import MAX_SAFE_INTEGER, UNDEFINED, canonical, is_finite, js_keys, js_sorted, stringify
from .validate import (UINT32_MAX, BASE64, array, attributes, check, enumeration, hex_id, int64, nullish, object_,
                       optional_string, optional_uint, string, uint)

KINDS = ["unspecified", "internal", "server", "client", "producer", "consumer"]


def group_key(group: dict) -> str:
    return stringify([group["service"], group["operation"], group["kind"]])


def span_key(trace_id: str, span_id: str) -> str:
    return f"{trace_id}/{span_id}"


def sorted_canonical(values) -> list:
    """Sort by canonical JSON in UTF-16 code-unit order (stable)."""
    return js_sorted(values, key=canonical)


def _populated(v) -> bool:
    if v is UNDEFINED or v is None or (isinstance(v, str) and v == ""):
        return False
    if isinstance(v, (list, dict)):
        return len(v) > 0
    return True


def normalize(input, options=UNDEFINED) -> dict:
    if options is UNDEFINED:
        options = {}
    object_(options, "normalize options", ["retainSpans"])
    if "retainSpans" in options:
        check(isinstance(options["retainSpans"], bool), "normalize options.retainSpans", "expected boolean")
    object_(input, "$")
    spans: dict = {}
    omitted: dict = {}
    warnings: list = []
    counts = {"input": 0, "duplicate": 0}

    def omit(path: str, value) -> None:
        if _populated(value):
            omitted[path] = omitted.get(path, 0) + 1

    def source_warnings(value, path: str) -> None:
        if value is UNDEFINED or value is None:
            return
        for v in array(value, path):
            string(v, path)
            warnings.append({"path": path, "message": v})

    def add(record: dict, raw: dict, path: str) -> None:
        counts["input"] += 1
        check(record["durationNs"] <= MAX_SAFE_INTEGER, path,
              "duration exceeds exact nanosecond limit (9007199254740991 ns)")
        key, fingerprint = span_key(record["traceId"], record["spanId"]), canonical(raw)
        if key in spans:
            check(spans[key]["fingerprint"] == fingerprint, path, f"conflicting duplicate identity {key}")
            counts["duplicate"] += 1
            return
        spans[key] = {**record, "fingerprint": fingerprint, "sourcePath": path}

    if "resourceSpans" in input:
        fmt = "otlp"
        object_(input, "$", ["resourceSpans"])
        for ri, rs in enumerate(array(input["resourceSpans"], "$.resourceSpans")):
            rp = f"$.resourceSpans[{ri}]"
            object_(rs, rp, ["resource", "scopeSpans", "schemaUrl"])
            optional_string(rs, "schemaUrl", rp)
            omit("resourceSpans.schemaUrl", rs.get("schemaUrl", UNDEFINED))
            resource = nullish(rs.get("resource", UNDEFINED), {})
            object_(resource, f"{rp}.resource", ["attributes", "droppedAttributesCount"])
            attrs = attributes(nullish(resource.get("attributes", UNDEFINED), []), f"{rp}.resource.attributes")
            optional_uint(resource, "droppedAttributesCount", rp, UINT32_MAX)
            omit("resource.droppedAttributesCount", resource.get("droppedAttributesCount", UNDEFINED))
            service_attr = next((a for a in attrs if a["key"] == "service.name"), None)
            check(service_attr is not None and "stringValue" in service_attr["value"], rp,
                  "resource requires string service.name")
            service = string(service_attr["value"]["stringValue"], f"{rp}.service.name", True)
            for attr in attrs:
                if attr["key"] != "service.name":
                    omit("resource.attributes.other", attr)
            for si, ss in enumerate(array(nullish(rs.get("scopeSpans", UNDEFINED), []), f"{rp}.scopeSpans")):
                sp = f"{rp}.scopeSpans[{si}]"
                object_(ss, sp, ["scope", "spans", "schemaUrl"])
                optional_string(ss, "schemaUrl", sp)
                omit("scopeSpans.schemaUrl", ss.get("schemaUrl", UNDEFINED))
                if "scope" in ss:
                    scope = ss["scope"]
                    object_(scope, f"{sp}.scope", ["name", "version", "attributes", "droppedAttributesCount"])
                    optional_string(scope, "name", sp)
                    optional_string(scope, "version", sp)
                    attributes(nullish(scope.get("attributes", UNDEFINED), []), f"{sp}.scope.attributes")
                    optional_uint(scope, "droppedAttributesCount", sp, UINT32_MAX)
                    omit("scopeSpans.scope", scope)
                for i, s in enumerate(array(nullish(ss.get("spans", UNDEFINED), []), f"{sp}.spans")):
                    p = f"{sp}.spans[{i}]"
                    object_(s, p, ["traceId", "spanId", "parentSpanId", "name", "kind", "startTimeUnixNano",
                                   "endTimeUnixNano", "status", "traceState", "flags", "attributes",
                                   "droppedAttributesCount", "events", "droppedEventsCount", "links",
                                   "droppedLinksCount"])
                    trace_id = hex_id(s.get("traceId", UNDEFINED), f"{p}.traceId", [32])
                    span_id = hex_id(s.get("spanId", UNDEFINED), f"{p}.spanId", [16])
                    parent_id = hex_id(s.get("parentSpanId", UNDEFINED), f"{p}.parentSpanId", [16], True)
                    operation = string(s.get("name", UNDEFINED), f"{p}.name", True)
                    kind = enumeration(nullish(s.get("kind", UNDEFINED), 0), f"{p}.kind", KINDS)
                    start = uint(s.get("startTimeUnixNano", UNDEFINED), f"{p}.startTimeUnixNano")
                    end = uint(s.get("endTimeUnixNano", UNDEFINED), f"{p}.endTimeUnixNano")
                    check(end >= start, p, "reversed timestamps")
                    status = nullish(s.get("status", UNDEFINED), {})
                    object_(status, f"{p}.status", ["code", "message"])
                    enumeration(nullish(status.get("code", UNDEFINED), 0), f"{p}.status.code",
                                ["unset", "ok", "error"])
                    optional_string(status, "message", p)
                    omit("span.status.message", status.get("message", UNDEFINED))
                    attributes(nullish(s.get("attributes", UNDEFINED), []), f"{p}.attributes")
                    omit("span.attributes", s.get("attributes", UNDEFINED))
                    optional_string(s, "traceState", p)
                    omit("span.traceState", s.get("traceState", UNDEFINED))
                    for k in ("flags", "droppedAttributesCount", "droppedEventsCount", "droppedLinksCount"):
                        optional_uint(s, k, p, UINT32_MAX)
                        omit(f"span.{k}", s.get(k, UNDEFINED))
                    for ei, e in enumerate(array(nullish(s.get("events", UNDEFINED), []), f"{p}.events")):
                        ep = f"{p}.events[{ei}]"
                        object_(e, ep, ["timeUnixNano", "name", "attributes", "droppedAttributesCount"])
                        uint(nullish(e.get("timeUnixNano", UNDEFINED), "0"), f"{ep}.timeUnixNano")
                        string(nullish(e.get("name", UNDEFINED), ""), f"{ep}.name")
                        attributes(nullish(e.get("attributes", UNDEFINED), []), f"{ep}.attributes")
                        optional_uint(e, "droppedAttributesCount", ep, UINT32_MAX)
                    omit("span.events", s.get("events", UNDEFINED))
                    for li, link in enumerate(array(nullish(s.get("links", UNDEFINED), []), f"{p}.links")):
                        lp = f"{p}.links[{li}]"
                        object_(link, lp, ["traceId", "spanId", "traceState", "attributes", "droppedAttributesCount",
                                           "flags"])
                        hex_id(link.get("traceId", UNDEFINED), f"{lp}.traceId", [32])
                        hex_id(link.get("spanId", UNDEFINED), f"{lp}.spanId", [16])
                        optional_string(link, "traceState", lp)
                        attributes(nullish(link.get("attributes", UNDEFINED), []), f"{lp}.attributes")
                        optional_uint(link, "droppedAttributesCount", lp, UINT32_MAX)
                        optional_uint(link, "flags", lp, UINT32_MAX)
                    omit("span.links", s.get("links", UNDEFINED))
                    raw = {"span": s, "resource": resource,
                           **({"scope": ss["scope"]} if "scope" in ss else {}),
                           **({"resourceSchemaUrl": rs["schemaUrl"]} if "schemaUrl" in rs else {}),
                           **({"scopeSchemaUrl": ss["schemaUrl"]} if "schemaUrl" in ss else {})}
                    add({"traceId": trace_id, "spanId": span_id,
                         "group": {"service": service, "operation": operation, "kind": kind},
                         "parents": [{"traceId": trace_id, "spanId": parent_id}] if parent_id else [],
                         "durationNs": end - start, "error": status.get("code") == 2}, raw, p)
    elif "data" in input:
        fmt = "jaeger"
        object_(input, "$", ["data", "total", "limit", "offset", "errors"])
        for k in ("total", "limit", "offset"):
            optional_uint(input, k, "$")
            omit(f"response.{k}", input.get(k, UNDEFINED))
        if input.get("errors") is not None:
            check(len(array(input["errors"], "$.errors")) == 0, "$.errors",
                  "upstream errors: partial/error responses are unsupported")
        for ti, trace in enumerate(array(input["data"], "$.data")):
            tp = f"$.data[{ti}]"
            object_(trace, tp, ["traceID", "spans", "processes", "warnings"])
            trace_id = hex_id(trace.get("traceID", UNDEFINED), f"{tp}.traceID", [16, 32])
            processes = object_(trace.get("processes", UNDEFINED), f"{tp}.processes")
            source_warnings(trace.get("warnings", UNDEFINED), f"{tp}.warnings")
            for key in js_keys(processes):
                proc, pp = processes[key], f"{tp}.processes.{key}"
                object_(proc, pp, ["serviceName", "tags"])
                string(proc.get("serviceName", UNDEFINED), f"{pp}.serviceName", True)
                _tags(nullish(proc.get("tags", UNDEFINED), []), f"{pp}.tags")
                omit("process.tags", proc.get("tags", UNDEFINED))
            for si, s in enumerate(array(trace.get("spans", UNDEFINED), f"{tp}.spans")):
                p = f"{tp}.spans[{si}]"
                object_(s, p, ["traceID", "spanID", "operationName", "references", "startTime", "duration", "tags",
                               "logs", "processID", "process", "warnings", "flags"])
                check(hex_id(s.get("traceID", UNDEFINED), f"{p}.traceID", [16, 32]) == trace_id, p,
                      "span traceID differs from enclosing trace")
                span_id = hex_id(s.get("spanID", UNDEFINED), f"{p}.spanID", [16])
                string(s.get("processID", UNDEFINED), f"{p}.processID", True)
                check(s["processID"] in processes, p, "unknown processID")
                proc = processes[s["processID"]]
                inline = s.get("process")
                if inline is not None:
                    object_(inline, f"{p}.process", ["serviceName", "tags"])
                    string(inline.get("serviceName", UNDEFINED), p, True)
                    _tags(nullish(inline.get("tags", UNDEFINED), []), p)
                    check(canonical(inline) == canonical(proc), p, "inline process conflicts with processID")
                span_tags = _tags(nullish(s.get("tags", UNDEFINED), []), f"{p}.tags")
                kind_tag = next((t for t in span_tags if t["key"] == "span.kind"), None)
                error_tag = next((t for t in span_tags if t["key"] == "error"), None)
                kind = nullish(kind_tag.get("value", UNDEFINED) if kind_tag is not None else UNDEFINED,
                               "unspecified")
                check(isinstance(kind, str) and kind in KINDS, f"{p}.span.kind", "unsupported span kind")
                if kind_tag is not None:
                    check(kind_tag["type"] == "string", p, "span.kind tag must have string type")
                if error_tag is not None:
                    check(error_tag["type"] == "bool", p, "error tag must have bool type")
                for t in span_tags:
                    if t["key"] not in ("span.kind", "error"):
                        omit("span.tags.other", t)
                parents, seen = [], set()
                for i, ref in enumerate(array(nullish(s.get("references", UNDEFINED), []), f"{p}.references")):
                    r = f"{p}.references[{i}]"
                    object_(ref, r, ["refType", "traceID", "spanID"])
                    ref_type = ref.get("refType")
                    check(isinstance(ref_type, str) and ref_type in ("CHILD_OF", "FOLLOWS_FROM"), r,
                          "unsupported reference type")
                    parent = {"traceId": hex_id(ref.get("traceID", UNDEFINED), f"{r}.traceID", [16, 32]),
                              "spanId": hex_id(ref.get("spanID", UNDEFINED), f"{r}.spanID", [16])}
                    if ref_type == "FOLLOWS_FROM":
                        omit("span.references.FOLLOWS_FROM", ref)
                        continue
                    key = span_key(parent["traceId"], parent["spanId"])
                    check(key not in seen, r, "duplicate CHILD_OF reference")
                    seen.add(key)
                    parents.append(parent)
                uint(s.get("startTime", UNDEFINED), f"{p}.startTime")
                duration = uint(s.get("duration", UNDEFINED), f"{p}.duration")
                optional_uint(s, "flags", p, UINT32_MAX)
                omit("span.flags", s.get("flags", UNDEFINED))
                for i, log in enumerate(array(nullish(s.get("logs", UNDEFINED), []), f"{p}.logs")):
                    lp = f"{p}.logs[{i}]"
                    object_(log, lp, ["timestamp", "fields"])
                    uint(log.get("timestamp", UNDEFINED), lp)
                    _tags(log.get("fields", UNDEFINED), lp)
                omit("span.logs", s.get("logs", UNDEFINED))
                source_warnings(s.get("warnings", UNDEFINED), f"{p}.warnings")
                operation = string(s.get("operationName", UNDEFINED), f"{p}.operationName", True)
                add({"traceId": trace_id, "spanId": span_id,
                     "group": {"service": proc["serviceName"], "operation": operation, "kind": kind},
                     "parents": parents, "durationNs": duration * 1000,
                     "error": error_tag is not None and error_tag.get("value") is True},
                    {"span": s, "process": proc}, p)
    else:
        check(False, "$", "unsupported trace wrapper: expected resourceSpans or data")
    check(len(spans) > 0, "$", "capture contains no spans")

    # Iterative DFS keeps deeply nested traces off the call stack.
    done: set = set()
    for start_key in spans:
        if start_key in done:
            continue
        active: set = set()
        stack = [(start_key, False)]
        while stack:
            key, leaving = stack.pop()
            if leaving:
                active.discard(key)
                done.add(key)
                continue
            if key in done or key not in spans:
                continue
            check(key not in active, "$", f"cyclic parent graph at {key}")
            active.add(key)
            stack.append((key, True))
            for parent in spans[key]["parents"]:
                stack.append((span_key(parent["traceId"], parent["spanId"]), False))

    groups: dict = {}
    edges: dict = {}
    missing_parents: list = []
    for span in spans.values():
        key = group_key(span["group"])
        if key not in groups:
            groups[key] = {"group": span["group"], "durations": [], "errorCount": 0}
        g = groups[key]
        g["durations"].append(span["durationNs"])
        g["errorCount"] += int(span["error"])
        for parent_ref in span["parents"]:
            parent = spans.get(span_key(parent_ref["traceId"], parent_ref["spanId"]))
            if parent is None:
                missing_parents.append({"traceId": span["traceId"], "spanId": span["spanId"],
                                        "parentTraceId": parent_ref["traceId"], "parentSpanId": parent_ref["spanId"]})
                continue
            edge_key = stringify([group_key(parent["group"]), key])
            if edge_key not in edges:
                edges[edge_key] = {"parent": parent["group"], "child": span["group"], "count": 0}
            edges[edge_key]["count"] += 1
    for g in groups.values():
        g["durations"].sort()
        g["count"] = len(g["durations"])
        g["p95Ns"] = g["durations"][math.ceil(95 * g["count"] / 100) - 1]
    return {"format": fmt, "groups": groups, "edges": edges, **({"spans": spans} if options.get("retainSpans") else {}),
            "spanCount": len(spans), "inputSpanCount": counts["input"], "duplicateSpanCount": counts["duplicate"],
            "traceCount": len({s["traceId"] for s in spans.values()}),
            "diagnostics": {"missingParents": sorted_canonical(missing_parents),
                            "omittedFields": [{"path": path, "occurrences": n}
                                              for path, n in js_sorted(omitted.items(), key=lambda kv: kv[0])],
                            "upstreamWarnings": sorted_canonical(warnings)}}


def _tags(value, path: str) -> list:
    seen: set = set()
    result = []
    for i, tag in enumerate(array(value, path)):
        p = f"{path}[{i}]"
        object_(tag, p, ["key", "type", "value"])
        string(tag.get("key", UNDEFINED), f"{p}.key", True)
        check(tag["key"] not in seen, p, f"duplicate tag {tag['key']}")
        seen.add(tag["key"])
        kind = tag.get("type")
        check(isinstance(kind, str) and kind in ("string", "bool", "int64", "float64", "binary"), p,
              "unsupported tag type")
        v = tag.get("value", UNDEFINED)
        if kind == "bool":
            check(isinstance(v, bool), p, "expected boolean tag value")
        if kind == "string":
            string(v, p)
        if kind == "binary":
            check(isinstance(v, str) and BASE64.fullmatch(v) is not None, p, "expected padded base64 binary tag")
        if kind == "int64":
            int64(v, p, "expected int64 tag value", "int64 tag out of range")
        if kind == "float64":
            check(is_finite(v), p, "expected finite float64 tag value")
        result.append(tag)
    return result
