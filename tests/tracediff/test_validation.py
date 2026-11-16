# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from otelwrench.tracediff import InputError, normalize, parse_json
from otelwrench.tracediff.jsontext import stringify

from .support import clone, hex_, jaeger, jaeger_span, otlp, otlp_span


def span(a):
    return a["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def _set(key, value):
    def change(a):
        span(a)[key] = value
    return change


def _delete(key):
    def change(a):
        del span(a)[key]
    return change


def _duration_outside_exact_range(a):
    span(a)["endTimeUnixNano"] = str(int(span(a)["startTimeUnixNano"]) + 9007199254740992)


def _status_code(a):
    span(a)["status"]["code"] = 3


def _missing_service(a):
    a["resourceSpans"][0]["resource"]["attributes"] = []


def _non_string_service(a):
    a["resourceSpans"][0]["resource"]["attributes"][0]["value"] = {"intValue": "1"}


def _duplicate_service(a):
    attrs = a["resourceSpans"][0]["resource"]["attributes"]
    attrs.append(attrs[0])


def _legacy_scope(a):
    a["resourceSpans"][0]["instrumentationLibrarySpans"] = []


def _conflicting_resource(a):
    copy = clone(a["resourceSpans"][0])
    copy["resource"]["attributes"][0]["value"]["stringValue"] = "another"
    a["resourceSpans"].append(copy)


def _two_span_cycle(a):
    span(a)["parentSpanId"] = hex_(2)
    a["resourceSpans"][0]["scopeSpans"][0]["spans"].append(otlp_span(span=2, parent=1))


MUTATIONS = [
    ("nonhex trace ID", _set("traceId", "z" * 32), r"hex ID"),
    ("wrong span width", _set("spanId", "ab"), r"hex ID"),
    ("zero ID", _set("spanId", "0" * 16), r"nonzero"),
    ("zero parent ID is not a root", _set("parentSpanId", "0" * 16), r"nonzero"),
    ("unsafe numeric timestamp", _set("startTimeUnixNano", 1700000000000000000), r"large values"),
    ("negative timestamp", _set("startTimeUnixNano", "-1"), r"unsigned"),
    ("fractional timestamp", _set("startTimeUnixNano", "1.5"), r"unsigned"),
    ("timestamp overflow", _set("endTimeUnixNano", str(1 << 64)), r"range"),
    ("duration outside exact range", _duration_outside_exact_range, r"duration exceeds"),
    ("missing operation", _delete("name"), r"nonempty"),
    ("empty operation", _set("name", " "), r"nonempty"),
    ("string kind enum", _set("kind", "SPAN_KIND_SERVER"), r"enum"),
    ("invalid status code", _status_code, r"enum"),
    ("unknown span field", _set("unexpected", 1), r"unsupported field"),
    ("missing service name", _missing_service, r"service.name"),
    ("non-string service name", _non_string_service, r"service.name"),
    ("duplicate service attributes", _duplicate_service, r"duplicate attribute"),
    ("multiple AnyValue variants", _set("attributes", [{"key": "x", "value": {"stringValue": "x", "boolValue": True}}]),
     r"AnyValue"),
    ("invalid attribute int64", _set("attributes", [{"key": "x", "value": {"intValue": "9223372036854775808"}}]),
     r"int64 out of range"),
    ("invalid event timestamp", _set("events", [{"timeUnixNano": "not-time"}]), r"unsigned"),
    ("invalid link ID", _set("links", [{"traceId": hex_(1, 32), "spanId": "bad"}]), r"hex ID"),
    ("legacy scope wrapper", _legacy_scope, r"unsupported field"),
    ("conflicting resource identity", _conflicting_resource, r"conflicting duplicate"),
    ("two-span parent cycle", _two_span_cycle, r"cyclic"),
]


@pytest.mark.parametrize("name,change,expected", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_reject_otlp(name, change, expected):
    a = otlp()
    change(a)
    with pytest.raises(InputError, match=expected):
        normalize(a)


def _jspan(key, value):
    def change(a):
        a["data"][0]["spans"][0][key] = value
    return change


def _jdelete(key):
    def change(a):
        del a["data"][0]["spans"][0][key]
    return change


def _errors(a):
    a["errors"] = [{"msg": "partial"}]


def _nonboolean_error_tag(a):
    a["data"][0]["spans"][0]["tags"][1] = {"key": "error", "type": "string", "value": "true"}


def _malformed_bool(a):
    a["data"][0]["spans"][0]["tags"][1]["value"] = 1


def _unknown_kind(a):
    a["data"][0]["spans"][0]["tags"][0]["value"] = "rpc"


def _duplicate_tags(a):
    tags = a["data"][0]["spans"][0]["tags"]
    tags.append(tags[0])


def _duplicate_refs(a):
    r = {"refType": "CHILD_OF", "traceID": hex_(1, 32), "spanID": hex_(2)}
    a["data"][0]["spans"][0]["references"] = [r, r]


def _conflicting_duration(a):
    a["data"][0]["spans"].append({**a["data"][0]["spans"][0], "duration": 999})


def _cross_trace_cycle(a):
    second = jaeger([jaeger_span(trace=2)])["data"][0]
    a["data"].append(second)
    a["data"][0]["spans"][0]["references"] = [{"refType": "CHILD_OF", "traceID": hex_(2, 32), "spanID": hex_(1)}]
    second["spans"][0]["references"] = [{"refType": "CHILD_OF", "traceID": hex_(1, 32), "spanID": hex_(1)}]


JAEGER_MUTATIONS = [
    ("negative duration", _jspan("duration", -1), r"unsigned"),
    ("fractional microseconds", _jspan("duration", 1.5), r"unsigned"),
    ("unsafe numeric duration", _jspan("duration", 1e20), r"unsigned"),
    ("error response", _errors, r"upstream errors"),
    ("nonmatching traceID", _jspan("traceID", hex_(2, 32)), r"differs"),
    ("unknown process", _jspan("processID", "missing"), r"unknown processID"),
    ("missing processID", _jdelete("processID"), r"nonempty"),
    ("nonboolean error tag", _nonboolean_error_tag, r"bool type"),
    ("malformed bool tag", _malformed_bool, r"boolean"),
    ("unknown kind", _unknown_kind, r"span kind"),
    ("duplicate tags", _duplicate_tags, r"duplicate tag"),
    ("unsupported reference", _jspan("references", [{"refType": "UNKNOWN", "traceID": hex_(1, 32),
                                                      "spanID": hex_(1)}]), r"reference type"),
    ("duplicate parent refs", _duplicate_refs, r"duplicate CHILD_OF"),
    ("conflicting duplicate duration", _conflicting_duration, r"conflicting duplicate"),
    ("inline process mismatch", _jspan("process", {"serviceName": "other"}), r"inline process conflicts"),
    ("deprecated parentSpanID", _jspan("parentSpanID", hex_(1)), r"unsupported field"),
    ("malformed log", _jspan("logs", [{"timestamp": -1, "fields": []}]), r"unsigned"),
    ("cross-trace cycle", _cross_trace_cycle, r"cyclic"),
]


@pytest.mark.parametrize("name,change,expected", JAEGER_MUTATIONS, ids=[m[0] for m in JAEGER_MUTATIONS])
def test_reject_jaeger(name, change, expected):
    a = jaeger()
    change(a)
    with pytest.raises(InputError, match=expected):
        normalize(a)


def test_jaeger_exact_duplicates_deduplicate_64_bit_trace_ids_zero_extend_case_normalizes():
    s = jaeger_span()
    s["traceID"], s["spanID"] = "abcdef0123456789", "ABCDEF0123456789"
    n = normalize(jaeger([s, clone(s)]))
    assert n["spanCount"] == 1 and n["duplicateSpanCount"] == 1 and n["traceCount"] == 1


def test_object_key_order_alone_does_not_conflict_for_duplicate_spans():
    s = otlp_span()
    reversed_span = dict(reversed(list(s.items())))
    assert normalize(otlp([s, reversed_span]))["duplicateSpanCount"] == 1


def test_missing_kind_and_status_use_unspecified_and_non_error_not_internal():
    a = otlp()
    del span(a)["kind"]
    del span(a)["status"]
    n = normalize(a)
    group = next(iter(n["groups"].values()))
    assert group["group"]["kind"] == "unspecified" and group["errorCount"] == 0


@pytest.mark.parametrize("name,a", [("array wrapper", []), ("unknown wrapper", {"traces": []}),
                                    ("empty OTLP", {"resourceSpans": []}), ("empty Jaeger", {"data": []}),
                                    ("mixed wrappers", {"resourceSpans": [], "data": []})])
def test_reject_wrapper(name, a):
    with pytest.raises(InputError, match=r"expected object|unsupported|no spans"):
        normalize(a)


def test_nested_anyvalue_metadata_validates_and_is_explicitly_omitted():
    a = otlp()
    span(a)["attributes"] = [{"key": "metadata", "value": {"kvlistValue": {"values": [{"key": "array", "value": {
        "arrayValue": {"values": [{"boolValue": True}, {"intValue": "-3"}, {"doubleValue": 1.5},
                                  {"bytesValue": "YQ=="}, {}]}}}]}}}]
    assert any(d["path"] == "span.attributes" for d in normalize(a)["diagnostics"]["omittedFields"])


BAD_JSON = ['{"a":1,"a":2}', '{"x":{"a":1,"\\u0061":2}}', '{"a":1,}', "[1,]", '{"a":01}', "true false", "NaN",
            "1e400", '"bad\nstring"', '"\\q"', '"unterminated', "{", "[", "", '{"a" 1}']


@pytest.mark.parametrize("bad", BAD_JSON, ids=[stringify(b) for b in BAD_JSON])
def test_strict_json_rejects(bad):
    with pytest.raises(InputError, match=r"JSON:"):
        parse_json(bad)


def test_strict_json_handles_escaped_quotes_unicode_key_safety_and_separate_object_scopes():
    text = '{"__proto__":{"x":1},"a":[{"b":"\\"\\\\\\u0061"},{"b":null}],"n":-1.25e2}'
    parsed = parse_json(text)
    assert parsed["n"] == -125 and parsed["a"][0]["b"] == '"\\a' and parsed["__proto__"] == {"x": 1}
    assert stringify(parsed) == stringify(json.loads(text))


def test_overdeep_json_is_rejected_with_an_explicit_error():
    with pytest.raises(InputError, match="nesting"):
        parse_json("[" * 258 + "0" + "]" * 258)


def test_oversized_decimal_integers_are_rejected_before_bigint_conversion():
    a = otlp()
    span(a)["endTimeUnixNano"] = "9" * 10000
    with pytest.raises(InputError, match="out of range"):
        normalize(a)
    b = otlp()
    span(b)["attributes"] = [{"key": "x", "value": {"intValue": "9" * 10000}}]
    with pytest.raises(InputError, match="int64 out of range"):
        normalize(b)
    c = jaeger()
    c["data"][0]["spans"][0]["tags"].append({"key": "x", "type": "int64", "value": "9" * 10000})
    with pytest.raises(InputError, match="int64 tag out of range"):
        normalize(c)


def test_kvlist_duplicate_keys_and_malformed_binary_tags_fail():
    a = otlp()
    span(a)["attributes"] = [{"key": "x", "value": {"kvlistValue": {"values": [{"key": "x", "value": {}},
                                                                               {"key": "x", "value": {}}]}}}]
    with pytest.raises(InputError, match="duplicate kvlist"):
        normalize(a)
    b = jaeger()
    b["data"][0]["spans"][0]["tags"].append({"key": "x", "type": "binary", "value": "%%%"})
    with pytest.raises(InputError, match="base64"):
        normalize(b)
