"""Explicit, deliberately bounded mappings to OTLP/HTTP JSON request bodies."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import re

from . import __version__
from .compression import DEFAULT_MAX_BYTES, decode_input
from .consistency import TraceConsistency
from .parser import ConversionError, decode_json, object_value, parse_input

UINT64_MAX = 18446744073709551615
MAX_SECONDS = Decimal("18446744073.709551615")
TIME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})[Tt]([0-9]{2}:[0-9]{2}:[0-9]{2})(\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})\Z")
LEVELS = {"debug": 5, "info": 9, "warning": 13, "error": 17, "fatal": 21}
ERROR_STATUSES = {"unknown_error", "invalid_argument", "deadline_exceeded", "not_found",
                  "already_exists", "permission_denied", "resource_exhausted",
                  "failed_precondition", "aborted", "out_of_range", "unimplemented",
                  "internal_error", "unavailable", "data_loss", "unauthenticated", "cancelled"}
LOSS_CATEGORIES = ("semantic_loss", "unsupported_field", "unsupported_item", "unused_metadata")


def _loss_category_selection(selection):
    if not isinstance(selection, (tuple, list, set, frozenset)):
        raise ConversionError("fail_on_loss_categories: expected a tuple, list, set or frozenset of category names")
    if any(not isinstance(name, str) for name in selection):
        raise ConversionError("fail_on_loss_categories: category names must be strings")
    names = set(selection)
    unknown = sorted(names.difference(LOSS_CATEGORIES))
    if unknown:
        raise ConversionError(f"fail_on_loss_categories: unsupported category {unknown[0]!r}; "
                              f"choose from {', '.join(LOSS_CATEGORIES)}")
    return tuple(sorted(names))


def string(value, where, nonempty=False):
    if not isinstance(value, str) or (nonempty and not value):
        raise ConversionError(f"{where}: expected {'nonempty ' if nonempty else ''}string")
    return value


def identifier(value, size, where):
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-fA-F]{{{size}}}", value) or int(value, 16) == 0:
        raise ConversionError(f"{where}: expected nonzero {size}-digit hex ID")
    return value.lower()


def event_identifier(value, where):
    string(value, where)
    compact = value
    if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        compact = value.replace("-", "")
    return identifier(compact, 32, where)


def _fraction_nanos(value, where):
    # Inspect decimal digits instead of building a possibly enormous denominator
    # for compact hostile inputs such as 1e-999999999.
    if value.is_zero():
        return 0
    _, digits, exponent = value.as_tuple()
    end = len(digits)
    while digits[end - 1] == 0:
        end -= 1
    power = exponent + len(digits) - end + 9
    if power < 0:
        raise ConversionError(f"{where}: sub-nanosecond precision is unsupported")
    if end + power > 20:
        raise ConversionError(f"{where}: timestamp outside unsigned 64-bit nanosecond range")
    return int("".join(map(str, digits[:end]))) * 10 ** power


def timestamp(value, where):
    """Exact nanoseconds, with no binary float or Decimal-context rounding."""
    if isinstance(value, str):
        match = TIME.fullmatch(value)
        if not match:
            raise ConversionError(f"{where}: expected RFC3339 timestamp with timezone")
        date, clock, fraction, zone = match.groups()
        try:
            offset = timedelta()
            if zone not in {"Z", "z"}:
                hours, minutes = int(zone[1:3]), int(zone[4:6])
                if hours > 23 or minutes > 59 or zone == "-00:00":
                    raise ValueError("invalid or unknown timezone offset")
                offset = timedelta(hours=hours, minutes=minutes)
                if zone[0] == "-":
                    offset = -offset
            dt = datetime.strptime(date + "T" + clock, "%Y-%m-%dT%H:%M:%S")
            delta = dt.replace(tzinfo=timezone(offset)) - datetime(1970, 1, 1, tzinfo=timezone.utc)
            seconds = delta.days * 86400 + delta.seconds
            frac = Decimal(fraction or "0")
            nanos = _fraction_nanos(frac, where)
            total = seconds * 1_000_000_000 + nanos
        except (ValueError, OverflowError) as exc:
            raise ConversionError(f"{where}: invalid RFC3339 timestamp: {exc}") from exc
    elif type(value) is int or isinstance(value, Decimal):
        value = Decimal(value)
        if not value.is_finite() or value < 0 or value > MAX_SECONDS:
            raise ConversionError(f"{where}: timestamp outside unsigned 64-bit nanosecond range")
        total = _fraction_nanos(value, where)
    else:
        raise ConversionError(f"{where}: expected numeric seconds or RFC3339 string")
    if not 0 <= total <= UINT64_MAX:
        raise ConversionError(f"{where}: timestamp outside unsigned 64-bit nanosecond range")
    return str(total)


def av(value):
    """Only called on explicitly selected, validated fields."""
    if isinstance(value, str):
        return {"stringValue": value}
    if type(value) is bool:
        return {"boolValue": value}
    if type(value) is int:
        return {"intValue": str(value)}
    if isinstance(value, list):
        return {"arrayValue": {"values": value}}
    if isinstance(value, dict):
        return {"kvlistValue": {"values": [attribute(k, v) for k, v in value.items()]}}
    raise TypeError(f"unvalidated attribute type: {type(value)}")


def attribute(key, value):
    return {"key": key, "value": av(value)}


class Report:
    def __init__(self):
        self.losses = []
        self.notes = []

    def drop(self, path, reason="unsupported field; not copied to OTLP", category="unsupported_field"):
        self.losses.append({"path": path, "category": category, "reason": reason})

    def extras(self, obj, known, path):
        for key in sorted(obj):
            if key not in known:
                # JSON Pointer escaping keeps arbitrary field names unambiguous.
                self.drop(pointer(path, key))
                self.losses[-1]["fieldCategory"] = key

    def note(self, path, reason):
        self.notes.append({"path": path, "reason": reason})


def pointer(base, key):
    return base + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _trace(event, report, required=False):
    contexts = object_value(event.get("contexts", {}), "/contexts")
    report.extras(contexts, {"trace"}, "/contexts")
    if "trace" not in contexts:
        if required:
            raise ConversionError("/contexts/trace: required for transactions")
        return {}
    return object_value(contexts["trace"], "/contexts/trace")


def _correlation(trace, where, required):
    result = {}
    has_trace = "trace_id" in trace
    has_span = "span_id" in trace
    if required or has_trace or has_span:
        if not has_trace or not has_span:
            raise ConversionError(f"{where}: trace_id and span_id must both be supplied")
        result["traceId"] = identifier(trace["trace_id"], 32, where + "/trace_id")
        result["spanId"] = identifier(trace["span_id"], 16, where + "/span_id")
    return result


def _common(event, envelope_headers):
    resources, attrs = [], []
    for field, key in [("release", "service.version"), ("environment", "deployment.environment.name")]:
        if field in event:
            resources.append(attribute(key, string(event[field], "/" + field)))
    event_id = event.get("event_id", envelope_headers.get("event_id"))
    if "event_id" in event:
        event_identifier(event["event_id"], "/event_id")
    if "event_id" in envelope_headers:
        header_id = event_identifier(envelope_headers["event_id"], "envelope/event_id")
        if "event_id" in event and event_identifier(event["event_id"], "/event_id") != header_id:
            raise ConversionError("event_id differs between envelope header and payload")
    if event_id is not None:
        attrs.append(attribute("sentry.event_id", event_id))
    return resources, attrs


def _exceptions(event, attrs, report):
    if "exception" not in event:
        return None
    exception = object_value(event["exception"], "/exception")
    report.extras(exception, {"values"}, "/exception")
    values = exception.get("values")
    if not isinstance(values, list) or not values:
        raise ConversionError("/exception/values: expected nonempty array")
    chain, body = [], None
    for i, raw in enumerate(values):
        path = f"/exception/values/{i}"
        exc = object_value(raw, path)
        report.extras(exc, {"type", "value", "stacktrace"}, path)
        entry = {}
        for key in ("type", "value"):
            if key in exc:
                entry[key] = string(exc[key], path + "/" + key)
        if not entry:
            raise ConversionError(f"{path}: exception needs type or value")
        body = ": ".join(entry[key] for key in ("type", "value") if key in entry)
        if "stacktrace" in exc:
            stack = object_value(exc["stacktrace"], path + "/stacktrace")
            report.extras(stack, {"frames"}, path + "/stacktrace")
            frames = stack.get("frames")
            if not isinstance(frames, list):
                raise ConversionError(f"{path}/stacktrace/frames: expected array")
            mapped_frames, lines = [], []
            for j, raw_frame in enumerate(frames):
                fp = f"{path}/stacktrace/frames/{j}"
                frame = object_value(raw_frame, fp)
                known = {"filename", "abs_path", "function", "module", "lineno", "colno", "in_app"}
                report.extras(frame, known, fp)
                mapped = {}
                for key in ("filename", "abs_path", "function", "module"):
                    if key in frame:
                        mapped[key] = string(frame[key], fp + "/" + key)
                for key in ("lineno", "colno"):
                    if key in frame:
                        number = frame[key]
                        if type(number) is not int or not 0 <= number <= 9223372036854775807:
                            raise ConversionError(f"{fp}/{key}: expected nonnegative int64")
                        mapped[key] = number
                if "in_app" in frame:
                    if type(frame["in_app"]) is not bool:
                        raise ConversionError(f"{fp}/in_app: expected boolean")
                    mapped["in_app"] = frame["in_app"]
                mapped_frames.append(av(mapped))
                lines.append(f"  File {mapped.get('filename', mapped.get('abs_path', '<unknown>'))!r}, "
                             f"line {mapped.get('lineno', '?')}, in {mapped.get('function', '<unknown>')}")
            entry["frames"] = mapped_frames
            entry["stacktrace"] = "\n".join(lines + [body])
            report.note(path + "/stacktrace", "rendered stack text; selected frame fields also retained structurally")
        chain.append(av(entry))
        # Last exception is the outermost in the SDK's oldest-first chain.
        if i == len(values) - 1:
            for source, target in [("type", "exception.type"), ("value", "exception.message"),
                                   ("stacktrace", "exception.stacktrace")]:
                if source in entry:
                    attrs.append(attribute(target, entry[source]))
    attrs.append(attribute("sentry.exception.chain", chain))
    report.note("/exception/values", "chain order preserved; standard exception attributes describe the last exception")
    return body


def convert_error(event, envelope_headers, report):
    known = {"type", "event_id", "release", "environment", "timestamp", "level", "message", "logentry", "exception", "contexts"}
    report.extras(event, known, "")
    resources, attrs = _common(event, envelope_headers)
    record = {"attributes": attrs}
    if "timestamp" in event:
        record["timeUnixNano"] = timestamp(event["timestamp"], "/timestamp")
    else:
        report.note("/timestamp", "absent; timeUnixNano omitted, no clock value invented")
    if "level" in event:
        level = string(event["level"], "/level")
        if level not in LEVELS:
            raise ConversionError(f"/level: unsupported Sentry level {level!r}")
        record.update(severityNumber=LEVELS[level], severityText=level)
    body = _exceptions(event, attrs, report)
    if "logentry" in event:
        logentry = object_value(event["logentry"], "/logentry")
        report.extras(logentry, {"message", "formatted"}, "/logentry")
        for key in ("message", "formatted"):
            if key in logentry:
                text = string(logentry[key], "/logentry/" + key)
                attrs.append(attribute("sentry.logentry." + key, text))
                body = text
    if "message" in event:
        body = string(event["message"], "/message")
        attrs.append(attribute("sentry.message", body))
    if body is None:
        raise ConversionError("error event needs message, logentry.message/formatted or exception")
    record["body"] = av(body)
    trace = _trace(event, report)
    record.update(_correlation(trace, "/contexts/trace", False))
    report.extras(trace, {"trace_id", "span_id"}, "/contexts/trace")
    return resources, record


def _span_fields(raw, trace, path, trace_path, attrs, report, name):
    span = _correlation(trace, trace_path, True)
    start = timestamp(raw.get("start_timestamp"), path + "/start_timestamp")
    end = timestamp(raw.get("timestamp"), path + "/timestamp")
    if int(end) < int(start):
        raise ConversionError(f"{path}/timestamp: end precedes start")
    span.update(name=name, kind=0, startTimeUnixNano=start, endTimeUnixNano=end, attributes=attrs)
    if "parent_span_id" in trace:
        span["parentSpanId"] = identifier(trace["parent_span_id"], 16, trace_path + "/parent_span_id")
        if span["parentSpanId"] == span["spanId"]:
            raise ConversionError(f"{trace_path}: span cannot parent itself")
    for field in ("op", "description", "status"):
        if field in trace:
            text = string(trace[field], trace_path + "/" + field)
            attrs.append(attribute("sentry." + field, text))
            if field == "status":
                code = 1 if text == "ok" else 2 if text in ERROR_STATUSES else 0
                span["status"] = {"code": code}
                report.note(trace_path + "/status", "original status retained; mapped to OTLP OK/ERROR/UNSET")
                if code == 0:
                    report.drop(trace_path + "/status", "unknown status retained as attribute, OTLP status left UNSET", "semantic_loss")
    return span


def convert_transaction(event, envelope_headers, report):
    known = {"type", "event_id", "release", "environment", "start_timestamp", "timestamp", "transaction", "contexts", "spans"}
    report.extras(event, known, "")
    resources, attrs = _common(event, envelope_headers)
    trace = _trace(event, report, True)
    trace_known = {"trace_id", "span_id", "parent_span_id", "op", "description", "status"}
    report.extras(trace, trace_known, "/contexts/trace")
    name = string(event.get("transaction"), "/transaction", nonempty=True)
    root = _span_fields(event, trace, "", "/contexts/trace", attrs, report, name)
    report.note("/contexts/trace", "kind is UNSPECIFIED (0); no span kind inferred from Sentry op")
    spans = [root]
    children = event.get("spans", [])
    if not isinstance(children, list):
        raise ConversionError("/spans: expected array")
    for i, raw in enumerate(children):
        path = f"/spans/{i}"
        child = object_value(raw, path)
        report.extras(child, trace_known | {"start_timestamp", "timestamp"}, path)
        if "description" in child:
            child_name = string(child["description"], path + "/description", nonempty=True)
        else:
            child_name = string(child.get("op"), path + "/op", nonempty=True)
        span = _span_fields(child, child, path, path, [], report, child_name)
        if span["traceId"] != root["traceId"]:
            raise ConversionError(f"{path}/trace_id: differs from transaction trace ID")
        if "parentSpanId" not in span:
            report.note(path + "/parent_span_id", "absent; no parent ID invented")
        spans.append(span)
    by_id = {}
    for span in spans:
        if span["spanId"] in by_id:
            raise ConversionError("transaction has duplicate span IDs")
        by_id[span["spanId"]] = span
    # Validate local ancestry without requiring externally referenced parents.
    done = set()
    for span in spans:
        seen, current = set(), span["spanId"]
        while current in by_id and current not in done:
            if current in seen:
                raise ConversionError("transaction has a cycle in parent span IDs")
            seen.add(current)
            current = by_id[current].get("parentSpanId")
        done.update(seen)
    return resources, spans


def convert_bytes(data: bytes, *, input_format="auto", skip_unsupported=False,
                  input_compression="none", max_input_bytes=DEFAULT_MAX_BYTES,
                  max_decompressed_bytes=DEFAULT_MAX_BYTES, fail_on_trace_conflicts=False,
                  fail_on_loss=False, fail_on_loss_categories=()):
    """Convert validated bytes; optionally reject trace conflicts or enumerated losses."""
    selected_categories = _loss_category_selection(fail_on_loss_categories)
    source = data
    data = decode_input(data, input_compression, max_input_bytes, max_decompressed_bytes)
    parsed = parse_input(data, input_format)
    logs, traces, entries = [], [], []
    consistency = TraceConsistency()
    header_report = Report()
    header_report.extras(parsed.headers, {"event_id"}, "/envelope")
    if "event_id" in parsed.headers:
        event_identifier(parsed.headers["event_id"], "envelope/event_id")
    for item in parsed.items:
        report = Report()
        report.extras(item.headers, {"type", "length"}, "/itemHeader")
        entry = item.metadata()
        if item.type not in {"event", "transaction"}:
            if not skip_unsupported:
                raise ConversionError(f"item {item.index}: unsupported item type {item.type!r}; use --skip-unsupported to record a skip")
            report.drop("/payload", "unsupported item explicitly skipped; payload not decoded", "unsupported_item")
            entry.update(status="skipped", outputLogRecords=0, outputSpans=0)
        else:
            event = object_value(decode_json(item.payload, f"item {item.index} payload"), f"item {item.index} payload")
            actual_type = event.get("type", "event")
            if actual_type != item.type:
                raise ConversionError(f"item {item.index}: payload type {actual_type!r} conflicts with item type {item.type!r}")
            try:
                if item.type == "event":
                    resource, record = convert_error(event, parsed.headers, report)
                    output_index = len(logs)
                    logs.append({"resource": {"attributes": resource}, "scopeLogs": [{"scope": _scope(), "logRecords": [record]}]})
                    entry.update(outputLogRecords=1, outputSpans=0, outputPointer=f"logs.json#/resourceLogs/{output_index}/scopeLogs/0/logRecords/0")
                    consistency.add_log(item.index, record, entry["outputPointer"])
                else:
                    resource, spans = convert_transaction(event, parsed.headers, report)
                    output_index = len(traces)
                    traces.append({"resource": {"attributes": resource}, "scopeSpans": [{"scope": _scope(), "spans": spans}]})
                    entry.update(outputLogRecords=0, outputSpans=len(spans), outputPointer=f"traces.json#/resourceSpans/{output_index}/scopeSpans/0/spans")
                    consistency.add_transaction(item.index, resource, spans, entry["outputPointer"])
            except ConversionError as exc:
                raise ConversionError(f"item {item.index}: {exc}") from exc
            entry["status"] = "converted"
        entry.update(losses=report.losses, notes=report.notes)
        entries.append(entry)
    converted = sum(entry["status"] == "converted" for entry in entries)
    if "event_id" in parsed.headers and not converted:
        header_report.drop("/envelope/event_id", "no converted item to carry envelope event ID", "unused_metadata")
    categories = Counter(loss["category"] for entry in entries for loss in entry["losses"])
    categories.update(loss["category"] for loss in header_report.losses)
    fields = Counter(loss["fieldCategory"] for entry in entries for loss in entry["losses"]
                     if loss["category"] == "unsupported_field")
    fields.update(loss["fieldCategory"] for loss in header_report.losses
                  if loss["category"] == "unsupported_field")
    manifest = {
        "schemaVersion": 1, "tool": {"name": "sentry2otlp", "version": __version__},
        "input": {"format": parsed.format, "bytes": bytes.__len__(source), "sha256": hashlib.sha256(source).hexdigest()},
        "summary": {"inputItems": len(entries), "convertedItems": converted,
                    "skippedItems": len(entries) - converted,
                    "logRecords": sum(e["outputLogRecords"] for e in entries),
                    "spans": sum(e["outputSpans"] for e in entries),
                    "lossEntries": sum(categories.values())},
        "lossCategories": dict(sorted(categories.items())),
        "unsupportedFieldCategories": dict(sorted(fields.items())),
        "envelopeLosses": header_report.losses, "items": entries,
        "traceConsistency": consistency.report(),
        "mappingPolicy": "Explicit subset; unknown field subtrees reported by JSON Pointer, values not copied. See README.md.",
    }
    if input_compression == "gzip":
        manifest["input"].update(compression="gzip", decompressedBytes=len(data),
                                 decompressedSha256=hashlib.sha256(data).hexdigest())
    # Keep policy after complete decoding, framing and mapping validation so
    # malformed later items or gzip trailers retain their authoritative errors.
    if fail_on_trace_conflicts:
        summary = manifest["traceConsistency"]["summary"]
        conflicts, cycles = summary["conflictingDuplicateGroups"], summary["cycles"]
        if conflicts or cycles:
            raise ConversionError(f"trace conflicts: conflicting duplicate groups={conflicts}, confirmed cycles={cycles}")
    if fail_on_loss and manifest["summary"]["lossEntries"]:
        raise ConversionError(f"conversion losses: loss entries={manifest['summary']['lossEntries']}")
    selected_total = sum(categories[name] for name in selected_categories)
    if selected_total:
        counts = ", ".join(f"{name}={categories[name]}" for name in selected_categories)
        raise ConversionError(f"conversion losses: {counts}; selected loss entries={selected_total}")
    return {"logs.json": {"resourceLogs": logs}, "traces.json": {"resourceSpans": traces}, "manifest.json": manifest}


def _scope():
    return {"name": "sentry2otlp", "version": __version__}
