# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from otelwrench.tracepack import JSBigInt, convert, parse_json, stringify

from .support import clone_fixture, jsdumps, same_float, simple_otlp, span

_MISSING = object()


def bad_otlp(modify, expected):
    doc = simple_otlp()
    modify(span(doc), doc)
    with pytest.raises(Exception, match=expected):
        convert(jsdumps(doc), {"to": "otlp"})


@pytest.mark.parametrize("name,key,value,error", [
    ("zero trace ID", "traceId", "0" * 32, "nonzero"),
    ("zero span ID", "spanId", "0" * 16, "nonzero"),
    ("zero parent ID", "parentSpanId", "0" * 16, "nonzero"),
    ("short OTLP trace ID", "traceId", "1" * 16, "32-digit"),
    ("base64 ID", "traceId", "AQEBAQEBAQEBAQEBAQEBAQ==", "hex ID"),
    ("non-hex ID", "spanId", "g" * 16, "hex ID"),
    ("missing name", "name", _MISSING, "expected string"),
    ("fractional time", "startTimeUnixNano", 1.5, "safe integer|exact integer"),
    ("negative time", "startTimeUnixNano", "-1", "outside range"),
    ("decimal time string", "startTimeUnixNano", "1.5", "decimal string"),
    ("exponent time string", "startTimeUnixNano", "1e9", "decimal string"),
    ("overflow nano time", "endTimeUnixNano", "18446744073709551616", "outside range"),
    ("time before start", "endTimeUnixNano", "0", "end before start"),
    ("self parent", "parentSpanId", "1111111111111111", "own parent"),
    ("invalid enum", "kind", 6, "outside range"),
    ("string enum", "kind", "SPAN_KIND_SERVER", "integer"),
    ("null event list", "events", None, "expected array"),
    ("null status", "status", None, "expected object"),
    ("invalid status code", "status", {"code": 3}, "outside range"),
])
def test_bad_otlp_field(name, key, value, error):
    def modify(s, _doc):
        if value is _MISSING:
            s.pop(key, None)  # JSON.stringify omits undefined members
        else:
            s[key] = value
    bad_otlp(modify, error)


def test_two_node_parent_cycle():
    def modify(s, doc):
        s["parentSpanId"] = "2222222222222222"
        doc["resourceSpans"][0]["scopeSpans"][0]["spans"].append({**s, "spanId": "2222222222222222",
                                                                  "parentSpanId": s["spanId"]})
    bad_otlp(modify, "parent cycle")


@pytest.mark.parametrize("attributes,expected", [
    ([{"key": "a", "value": {"intValue": "1"}}, {"key": "a", "value": {"intValue": "2"}}], "duplicate attribute"),
    ([{"key": "a", "value": {"bytesValue": ""}}, {"key": "a", "value": {"stringValue": "x"}}], "duplicate attribute"),
    ([{"key": "a", "value": {"stringValue": "", "boolValue": True}}], "exactly one"),
    ([{"key": "a", "value": {"boolValue": "true"}}], "expected boolean"),
    ([{"key": "a", "value": {"intValue": "9223372036854775808"}}], "outside range"),
], ids=["duplicate attribute key", "duplicate dropped attribute key", "ambiguous AnyValue", "wrong primitive type",
        "int64 overflow"])
def test_bad_otlp_attributes(attributes, expected):
    def modify(s, _doc):
        s["attributes"] = attributes
    bad_otlp(modify, expected)


def test_invalid_service_name_type():
    def modify(_s, doc):
        doc["resourceSpans"][0]["resource"]["attributes"][0]["value"] = {"intValue": 1}
    bad_otlp(modify, "must be a string")


def _multiple(s):
    s["references"].append(dict(s["references"][0]))


def _set(key, value):
    def modify(s):
        s[key] = value
    return modify


def _ref(key, value):
    def modify(s):
        s["references"][0][key] = value
    return modify


@pytest.mark.parametrize("name,modify,expected", [
    ("multiple references", _multiple, "multiple Jaeger references"),
    ("FOLLOWS_FROM", _ref("refType", "FOLLOWS_FROM"), "only CHILD_OF"),
    ("cross-trace reference", _ref("traceID", "1" * 32), "cross-trace"),
    ("unknown process", _set("processID", "absent"), "unknown process"),
    ("negative duration", _set("duration", -1), "outside range"),
    ("trace ID mismatch", _set("traceID", "1" * 32), "does not match"),
    ("unknown tag type", _set("tags", [{"key": "x", "type": "object", "value": {}}]), "unsupported Jaeger tag type"),
    ("bad span kind convention", _set("tags", [{"key": "span.kind", "type": "string", "value": "unknown"}]),
     "unsupported span.kind"),
    ("bad status convention", _set("tags", [{"key": "otel.status_code", "type": "string", "value": "INVALID"}]),
     r"unsupported otel\.status_code"),
])
def test_jaeger_invalid(name, modify, expected):
    doc = clone_fixture("common.jaeger.json")
    modify(doc["data"][0]["spans"][1])
    with pytest.raises(Exception, match=expected):
        convert(jsdumps(doc), {"to": "otlp"})


def _delete(key):
    def modify(s):
        del s[key]
    return modify


def _end_over(s):
    s["timestamp"] = "18446744073709551"
    s["duration"] = 1


@pytest.mark.parametrize("name,modify,expected", [
    ("shared spans", _set("shared", True), "shared spans unsupported"),
    ("unknown kind", _set("kind", "INTERNAL"), "unsupported Zipkin kind"),
    ("missing timestamp", _delete("timestamp"), "integer or decimal string"),
    ("missing duration", _delete("duration"), "integer or decimal string"),
    ("nonstring tags", _set("tags", {"number": 3}), "expected string"),
    ("microseconds overflow", _set("timestamp", "18446744073709552"), "outside range"),
    ("end over uint64 nanos", _end_over, "end exceeds"),
])
def test_zipkin_invalid(name, modify, expected):
    doc = clone_fixture("common.zipkin.json")[:1]
    modify(doc[0])
    with pytest.raises(Exception, match=expected):
        convert(jsdumps(doc), {"to": "otlp"})


@pytest.mark.parametrize("key", ["otel.status_code", "otel.status_description", "span.kind"])
def test_jaeger_output_rejects_reserved_tag_collision(key):
    doc = simple_otlp()
    span(doc)["attributes"] = [{"key": key, "value": {"stringValue": "arbitrary"}}]
    with pytest.raises(Exception, match="collides with Jaeger convention"):
        convert(jsdumps(doc), {"to": "jaeger"})


def test_jaeger_output_rejects_event_attribute_name_collision():
    doc = simple_otlp()
    span(doc)["events"] = [{"timeUnixNano": "1700000000000000000", "name": "n",
                            "attributes": [{"key": "event", "value": {"stringValue": "different"}}]}]
    with pytest.raises(Exception, match="collides with Jaeger log event"):
        convert(jsdumps(doc), {"to": "jaeger"})


@pytest.mark.parametrize("text", ["{}", "null", "42", '{"data":[],"resourceSpans":[]}', "[[{}]]"])
def test_unsupported_or_ambiguous_format(text):
    with pytest.raises(Exception, match="cannot detect|expected object"):
        convert(text, {"to": "otlp"})


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{"a":1,"\\u0061":2}', "[1,]", '{"x":1,}', '{"x":01}', "true null",
                                  '"unterminated', '{"x":"\\x"}', "[NaN]", "1e400"])
def test_reject_invalid_or_unsafe_json(text):
    with pytest.raises(Exception):
        parse_json(text)


def test_parser_nesting_limit_and_prototype_safe_keys():
    with pytest.raises(Exception, match="nesting"):
        parse_json("[" * 130 + "0" + "]" * 130)
    x = parse_json('{"__proto__":{"polluted":true},"constructor":"safe","large":9007199254740993}')
    assert type(x) is dict
    assert x["large"] == 9007199254740993 and type(x["large"]) is JSBigInt
    assert json.loads(stringify(x))["__proto__"] == {"polluted": True}


def test_explicit_wrong_source_and_unsupported_target_fail():
    with pytest.raises(Exception, match="expected object"):
        convert("[]", {"from": "otlp", "to": "otlp"})
    with pytest.raises(Exception, match="--to"):
        convert("[]", {"to": "unknown"})
    with pytest.raises(Exception, match="--from"):
        convert("[]", {"from": "unknown", "to": "otlp"})


def test_fractional_numeric_token_cannot_round_into_a_valid_integer_time():
    for token in ["1.00000000000000001", "9007199254740991.1", "1e-400"]:
        text = jsdumps(simple_otlp()).replace('"1700000000000000000"', token)
        with pytest.raises(Exception, match="exact integer numeric token"):
            convert(text, {"to": "otlp"})


def test_json_serializer_preserves_negative_zero_for_float_attributes():
    doc = simple_otlp()
    span(doc)["attributes"] = [{"key": "zero", "value": {"doubleValue": 0}}]
    text = jsdumps(doc).replace('"doubleValue":0', '"doubleValue":-0')
    result = convert(text, {"to": "otlp", "strictLoss": True})
    assert same_float(span(result["output"])["attributes"][0]["value"]["doubleValue"], -0.0)
    assert '"doubleValue": -0' in stringify(result["output"])


def test_decimal_numeric_tokens_cannot_masquerade_as_resource_or_scope_objects():
    resource = simple_otlp()
    resource["resourceSpans"][0]["resource"] = 1.5
    with pytest.raises(Exception, match="expected object"):
        convert(jsdumps(resource), {"to": "otlp"})
    scope = simple_otlp()
    scope["resourceSpans"][0]["scopeSpans"] = [1.5]
    with pytest.raises(Exception, match="expected object"):
        convert(jsdumps(scope), {"to": "otlp"})
