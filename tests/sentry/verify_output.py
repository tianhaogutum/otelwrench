"""Independent subset schema/invariant checker; imports no sentry2otlp modules.

Run: python -m tests.sentry.verify_output out --source examples/sentry/error.envelope
This checks our supported OTLP JSON subset, not the entire upstream schema.
"""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import re


def keys(obj, required, optional=()):
    assert isinstance(obj, dict), f"expected object, got {obj!r}"
    assert set(required) <= set(obj), f"missing keys: {set(required) - set(obj)}"
    assert set(obj) <= set(required) | set(optional), f"unknown keys: {set(obj) - set(required) - set(optional)}"


def integer_string(value, minimum=0, maximum=2**64 - 1):
    assert isinstance(value, str) and re.fullmatch(r"0|[1-9]\d*", value), value
    assert minimum <= int(value) <= maximum, value


def hex_id(value, length):
    assert isinstance(value, str) and re.fullmatch(rf"[a-f0-9]{{{length}}}", value), value
    assert int(value, 16) != 0


def any_value(value):
    assert isinstance(value, dict) and len(value) == 1
    field, data = next(iter(value.items()))
    if field == "stringValue":
        assert isinstance(data, str)
    elif field == "boolValue":
        assert type(data) is bool
    elif field == "intValue":
        integer_string(data, maximum=2**63 - 1)
    elif field == "arrayValue":
        keys(data, {"values"})
        assert isinstance(data["values"], list)
        for child in data["values"]:
            any_value(child)
    elif field == "kvlistValue":
        keys(data, {"values"})
        attributes(data["values"])
    else:
        raise AssertionError(f"unexpected AnyValue variant {field}")


def attributes(values):
    assert isinstance(values, list)
    seen = set()
    for attr in values:
        keys(attr, {"key", "value"})
        assert isinstance(attr["key"], str)
        assert attr["key"] not in seen, f"duplicate attribute {attr['key']}"
        seen.add(attr["key"])
        any_value(attr["value"])


def resource_groups(document, signal):
    field = "resourceLogs" if signal == "logs" else "resourceSpans"
    scope_field = "scopeLogs" if signal == "logs" else "scopeSpans"
    records_field = "logRecords" if signal == "logs" else "spans"
    keys(document, {field})
    assert isinstance(document[field], list)
    records = []
    for group in document[field]:
        keys(group, {"resource", scope_field})
        keys(group["resource"], {"attributes"})
        attributes(group["resource"]["attributes"])
        assert isinstance(group[scope_field], list) and group[scope_field]
        for scope in group[scope_field]:
            keys(scope, {"scope", records_field})
            keys(scope["scope"], {"name", "version"})
            assert scope["scope"]["name"] == "sentry2otlp"
            assert isinstance(scope["scope"]["version"], str)
            assert isinstance(scope[records_field], list) and scope[records_field]
            for record in scope[records_field]:
                attributes(record["attributes"])
                if signal == "logs":
                    keys(record, {"body", "attributes"}, {"timeUnixNano", "severityNumber", "severityText", "traceId", "spanId"})
                    any_value(record["body"])
                    if "timeUnixNano" in record:
                        integer_string(record["timeUnixNano"])
                    if "severityNumber" in record:
                        assert type(record["severityNumber"]) is int and record["severityNumber"] in {5, 9, 13, 17, 21}
                        assert record["severityText"] in {"debug", "info", "warning", "error", "fatal"}
                    assert ("traceId" in record) == ("spanId" in record)
                else:
                    keys(record, {"name", "traceId", "spanId", "kind", "startTimeUnixNano", "endTimeUnixNano", "attributes"}, {"parentSpanId", "status"})
                    assert isinstance(record["name"], str) and record["name"]
                    assert type(record["kind"]) is int and record["kind"] == 0
                    integer_string(record["startTimeUnixNano"])
                    integer_string(record["endTimeUnixNano"])
                    assert int(record["endTimeUnixNano"]) >= int(record["startTimeUnixNano"])
                    if "parentSpanId" in record:
                        hex_id(record["parentSpanId"], 16)
                        assert record["parentSpanId"] != record["spanId"]
                    if "status" in record:
                        keys(record["status"], {"code"})
                        assert type(record["status"]["code"]) is int and record["status"]["code"] in {0, 1, 2}
                if "traceId" in record:
                    hex_id(record["traceId"], 32)
                    hex_id(record["spanId"], 16)
                records.append(record)
    return records


def _resolve(pointer, documents):
    filename, path = pointer.split("#", 1)
    current = documents[filename]
    for token in path.lstrip("/").split("/"):
        current = current[int(token)] if isinstance(current, list) else current[token]
    return current


def trace_consistency(report, documents, entries, spans):
    keys(report, {"summary", "duplicates", "parents", "cycles", "uncorrelatedLogs"})
    summary = report["summary"]
    keys(summary, {"traceIds", "spanOccurrences", "spanIdentities", "identicalDuplicateGroups",
                   "conflictingDuplicateGroups", "parents", "cycles", "uncorrelatedLogs"})
    definitions, locations = {}, {}
    uncorrelated = []
    for entry in entries:
        if entry["status"] != "converted":
            continue
        if entry["type"] == "event":
            if "traceId" not in _resolve(entry["outputPointer"], documents):
                uncorrelated.append({"itemIndex": entry["index"], "path": "/contexts/trace",
                                     "outputPointer": entry["outputPointer"]})
            continue
        resource_pointer = entry["outputPointer"].split("/scopeSpans/")[0] + "/resource"
        resource = _resolve(resource_pointer, documents)
        for index, span in enumerate(_resolve(entry["outputPointer"], documents)):
            location = {"itemIndex": entry["index"],
                        "path": "/contexts/trace" if index == 0 else f"/spans/{index - 1}",
                        "outputPointer": entry["outputPointer"] + f"/{index}"}
            locations[location["outputPointer"]] = location
            identity = (span["traceId"], span["spanId"])
            definitions.setdefault(identity, []).append((span, resource, location))
    assert summary["spanOccurrences"] == len(spans)
    assert summary["spanIdentities"] == len(definitions)
    assert summary["traceIds"] == len({span["traceId"] for span in spans})
    assert report["uncorrelatedLogs"] == uncorrelated
    assert summary["uncorrelatedLogs"] == len(uncorrelated)
    expected_duplicates, conflicts = {}, set()
    for identity, occurrences in definitions.items():
        if len(occurrences) > 1:
            kind = "identical" if all(o[:2] == occurrences[0][:2] for o in occurrences) else "conflicting"
            expected_duplicates[identity] = {"kind": kind, "traceId": identity[0], "spanId": identity[1],
                                             "occurrences": [o[2] for o in occurrences]}
            if kind == "conflicting":
                conflicts.add(identity)
    assert report["duplicates"] == list(expected_duplicates.values())
    for kind in ("identical", "conflicting"):
        assert summary[kind + "DuplicateGroups"] == sum(d["kind"] == kind for d in report["duplicates"])
    assert len(report["parents"]) == len(spans)
    assert [p["outputPointer"] for p in report["parents"]] == list(locations)
    states = {"not_supplied", "resolved", "ambiguous", "missing", "other_trace_only"}
    supplied_ids = {s["spanId"] for s in spans}
    for relationship in report["parents"]:
        keys(relationship, {"itemIndex", "path", "outputPointer", "traceId", "spanId", "status"}, {"parentSpanId"})
        span = _resolve(relationship["outputPointer"], documents)
        for field, value in locations[relationship["outputPointer"]].items():
            assert relationship[field] == value
        assert relationship["traceId"] == span["traceId"]
        assert relationship["spanId"] == span["spanId"]
        assert relationship.get("parentSpanId") == span.get("parentSpanId")
        assert ("parentSpanId" in relationship) == ("parentSpanId" in span)
        status = relationship["status"]
        assert status in states
        target = (span["traceId"], span.get("parentSpanId"))
        if status == "not_supplied":
            assert "parentSpanId" not in span
        elif status == "ambiguous":
            assert target in conflicts
        elif status == "resolved":
            assert target in definitions and target not in conflicts
        else:
            assert "parentSpanId" in span and target not in definitions
            assert (span["parentSpanId"] in supplied_ids) == (status == "other_trace_only")
    assert summary["parents"] == {state: sum(p["status"] == state for p in report["parents"]) for state in states}
    seen_cycles = set()
    for cycle in report["cycles"]:
        keys(cycle, {"traceId", "spanIds"})
        ids = cycle["spanIds"]
        assert len(ids) >= 2 and len(ids) == len(set(ids)) and ids[0] == min(ids)
        identity = (cycle["traceId"], tuple(ids))
        assert identity not in seen_cycles
        seen_cycles.add(identity)
        for child, parent in zip(ids, ids[1:] + ids[:1]):
            key = (cycle["traceId"], child)
            assert key not in conflicts
            assert definitions[key][0][0]["parentSpanId"] == parent
    assert summary["cycles"] == len(report["cycles"])


def verify_documents(documents, source=None):
    assert set(documents) == {"logs.json", "traces.json", "manifest.json"}
    logs = resource_groups(documents["logs.json"], "logs")
    spans = resource_groups(documents["traces.json"], "traces")
    manifest = documents["manifest.json"]
    assert manifest["schemaVersion"] == 1
    input_metadata = manifest["input"]
    payload_source = source
    if source is not None:
        assert input_metadata["bytes"] == len(source)
        assert input_metadata["sha256"] == hashlib.sha256(source).hexdigest()
    if "compression" in input_metadata:
        assert input_metadata["compression"] == "gzip"
        assert type(input_metadata["decompressedBytes"]) is int and input_metadata["decompressedBytes"] >= 0
        assert re.fullmatch(r"[0-9a-f]{64}", input_metadata["decompressedSha256"])
        if source is not None:
            # Independent standard-library decoding for trusted test fixtures.
            # Item offsets always refer to the decompressed envelope/JSON bytes.
            payload_source = gzip.decompress(source)
            assert input_metadata["decompressedBytes"] == len(payload_source)
            assert input_metadata["decompressedSha256"] == hashlib.sha256(payload_source).hexdigest()
    summary = manifest["summary"]
    entries = manifest["items"]
    assert summary["inputItems"] == len(entries)
    assert [e["index"] for e in entries] == list(range(len(entries)))
    assert summary["convertedItems"] == sum(e["status"] == "converted" for e in entries)
    assert summary["skippedItems"] == sum(e["status"] == "skipped" for e in entries)
    assert summary["inputItems"] == summary["convertedItems"] + summary["skippedItems"]
    assert summary["logRecords"] == len(logs) == sum(e["outputLogRecords"] for e in entries)
    assert summary["spans"] == len(spans) == sum(e["outputSpans"] for e in entries)
    all_losses = manifest["envelopeLosses"] + [loss for e in entries for loss in e["losses"]]
    assert manifest["lossCategories"] == dict(Counter(loss["category"] for loss in all_losses))
    assert summary["lossEntries"] == len(all_losses)
    assert manifest["unsupportedFieldCategories"] == dict(Counter(loss["fieldCategory"] for loss in all_losses if loss["category"] == "unsupported_field"))
    for loss in all_losses:
        assert isinstance(loss["path"], str) and loss["path"].startswith("/")
        assert not re.search(r"~(?![01])", loss["path"]), loss
        assert isinstance(loss["reason"], str) and loss["reason"]
    last_end = 0
    for entry in entries:
        assert entry["status"] in {"converted", "skipped"}
        assert entry["payloadOffset"] >= last_end
        last_end = entry["payloadOffset"] + entry["payloadBytes"]
        if payload_source is not None:
            payload = payload_source[entry["payloadOffset"]:last_end]
            assert len(payload) == entry["payloadBytes"]
            assert hashlib.sha256(payload).hexdigest() == entry["payloadSha256"]
        if entry["status"] == "skipped":
            assert entry["outputSpans"] == entry["outputLogRecords"] == 0
            assert "outputPointer" not in entry
        elif entry["type"] == "event":
            assert entry["outputLogRecords"] == 1 and entry["outputSpans"] == 0
            assert _resolve(entry["outputPointer"], documents) in logs
        elif entry["type"] == "transaction":
            assert entry["outputLogRecords"] == 0 and entry["outputSpans"] >= 1
            resolved = _resolve(entry["outputPointer"], documents)
            assert len(resolved) == entry["outputSpans"]
            assert all(span in spans for span in resolved)
        else:
            raise AssertionError("unsupported item claimed converted")
    trace_consistency(manifest["traceConsistency"], documents, entries, spans)
    return {"items": len(entries), "logs": len(logs), "spans": len(spans), "losses": len(all_losses)}


def verify_directory(directory, source=None):
    directory = Path(directory)
    assert {p.name for p in directory.iterdir()} == {"logs.json", "traces.json", "manifest.json"}
    documents = {name: json.loads((directory / name).read_text(encoding="utf-8")) for name in ("logs.json", "traces.json", "manifest.json")}
    return verify_documents(documents, source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--expected-items", type=int)
    parser.add_argument("--expected-logs", type=int)
    parser.add_argument("--expected-spans", type=int)
    args = parser.parse_args()
    result = verify_directory(args.directory, args.source.read_bytes() if args.source else None)
    for field in ("items", "logs", "spans"):
        expected = getattr(args, "expected_" + field)
        if expected is not None:
            assert result[field] == expected, (field, result[field], expected)
    print(json.dumps({"verified": True, **result}))


if __name__ == "__main__":
    main()
