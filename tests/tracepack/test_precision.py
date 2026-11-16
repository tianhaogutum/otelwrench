# SPDX-License-Identifier: Apache-2.0
import re

import pytest

from otelwrench.tracepack import JSBigInt, StrictLossError, convert, parse_json, stringify

from .support import clone_fixture, jsdumps, same_float, simple_otlp, span


def to_otlp(doc):
    return convert(jsdumps(doc), {"to": "otlp", "strictLoss": True})


@pytest.mark.parametrize("value", ["9007199254740991", "9007199254740992", "9007199254740993", "18446744073709551615"])
def test_exact_quoted_otlp_nanoseconds(value):
    doc = simple_otlp()
    span(doc)["startTimeUnixNano"] = value
    span(doc)["endTimeUnixNano"] = value
    output = to_otlp(doc)["output"]
    assert span(output)["startTimeUnixNano"] == value
    assert span(output)["endTimeUnixNano"] == value


def test_unsafe_unquoted_otlp_nanoseconds_rejected_even_when_parser_can_retain_token():
    text = jsdumps(simple_otlp()).replace('"1700000000000000000"', "1700000000000000001")
    with pytest.raises(Exception, match="unsafe numeric nanos"):
        convert(text, {"to": "otlp"})


def test_safe_numeric_otlp_nanos_accepted_without_loss():
    doc = simple_otlp()
    span(doc)["startTimeUnixNano"] = 9007199254740000
    span(doc)["endTimeUnixNano"] = 9007199254740991
    assert span(to_otlp(doc)["output"])["endTimeUnixNano"] == "9007199254740991"


@pytest.mark.parametrize("source", ["zipkin", "jaeger"])
@pytest.mark.parametrize("raw", ["9007199254740991", "9007199254740992", "9007199254740993"])
def test_exact_numeric_microseconds_survive_to_otlp_and_jaeger(source, raw):
    if source == "zipkin":
        data = clone_fixture("common.zipkin.json")[:1]
        data[0]["timestamp"] = "REPLACE"
        data[0]["duration"] = 1
        data[0]["annotations"] = []
    else:
        data = clone_fixture("common.jaeger.json")
        data["data"][0]["spans"] = data["data"][0]["spans"][:1]
        del data["data"][0]["processes"]["p1"]
        data["data"][0]["spans"][0]["startTime"] = "REPLACE"
        data["data"][0]["spans"][0]["duration"] = 1
        data["data"][0]["spans"][0]["logs"] = []
    text = jsdumps(data).replace('"REPLACE"', raw, 1)
    out = convert(text, {"from": source, "to": "otlp", "strictLoss": True})["output"]
    assert span(out)["startTimeUnixNano"] == str(int(raw) * 1000)
    assert span(out)["endTimeUnixNano"] == str(int(raw) * 1000 + 1000)
    back = convert(stringify(out), {"to": "jaeger", "strictLoss": True})["output"]
    assert re.search(f'"startTime": {raw},', stringify(back))
    assert int(parse_json(stringify(back))["data"][0]["spans"][0]["startTime"]) == int(raw)


def test_sub_microsecond_values_report_original_source_paths_and_floor_values():
    doc = simple_otlp()
    s = span(doc)
    s["startTimeUnixNano"] = "1700000000000000999"
    s["endTimeUnixNano"] = "1700000000000002000"
    s["events"] = [{"timeUnixNano": "1700000000000001001", "name": "tick"}]
    result = convert(jsdumps(doc), {"to": "jaeger"})
    out = result["output"]["data"][0]["spans"][0]
    assert out["startTime"] == 1700000000000000
    assert out["duration"] == 1
    assert out["logs"][0]["timestamp"] == 1700000000000001
    assert [x["path"] for x in result["report"]["losses"]] == [
        "$.resourceSpans[0].scopeSpans[0].spans[0].endTimeUnixNano",
        "$.resourceSpans[0].scopeSpans[0].spans[0].events[0].timeUnixNano",
        "$.resourceSpans[0].scopeSpans[0].spans[0].startTimeUnixNano",
    ]
    with pytest.raises(StrictLossError, match="rejected 3"):
        convert(jsdumps(doc), {"to": "jaeger", "strictLoss": True})


@pytest.mark.parametrize("source", ["jaeger", "zipkin"])
@pytest.mark.parametrize("length", [16, 32])
def test_trace_id_padding_and_normalization(source, length):
    raw = "ABCDEF0123456789" * (length // 16)
    if source == "zipkin":
        doc = clone_fixture("common.zipkin.json")
        for s in doc:
            s["traceId"] = raw
    else:
        doc = clone_fixture("common.jaeger.json")
        doc["data"][0]["traceID"] = raw
        for s in doc["data"][0]["spans"]:
            s["traceID"] = raw
            for r in s["references"]:
                r["traceID"] = raw
    out = to_otlp(doc)["output"]
    for r in out["resourceSpans"]:
        assert r["scopeSpans"][0]["spans"][0]["traceId"] == raw.lower().rjust(32, "0")


def test_exact_exponent_decimal_microsecond_tokens_use_source_digits():
    data = clone_fixture("common.zipkin.json")[:1]
    data[0]["annotations"] = []
    text = jsdumps(data).replace("1700000000000000", "9.007199254740993e15", 1)
    out = convert(text, {"to": "otlp", "strictLoss": True})["output"]
    assert span(out)["startTimeUnixNano"] == "9007199254740993000"


def test_float64_attributes_retain_ieee754_values_across_exponents_and_the_finite_range():
    for token in ["1e20", "1e308", "1.7976931348623157e308", "5e-324", "0.125", "-0.0", "9007199254740993"]:
        doc = simple_otlp()
        span(doc)["attributes"] = [{"key": "double", "value": {"doubleValue": "TOKEN"}}]
        text = jsdumps(doc).replace('"TOKEN"', token)
        jaeger = convert(text, {"to": "jaeger", "strictLoss": True})
        out = convert(stringify(jaeger["output"]), {"to": "otlp", "strictLoss": True})
        assert same_float(span(out["output"])["attributes"][0]["value"]["doubleValue"], float(token)), token


def test_parse_json_marks_unsafe_integers():
    assert type(parse_json("9007199254740993")) is JSBigInt
