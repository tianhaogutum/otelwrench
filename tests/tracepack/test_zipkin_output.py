# SPDX-License-Identifier: Apache-2.0
import json
import re

import pytest

from otelwrench.tracepack import StrictLossError, convert, parse_json, stringify

from .support import clone_fixture, fixture, jsdumps, simple_otlp, span

P = "$.resourceSpans[0].scopeSpans[0].spans[0]"


def zipkin(doc):
    return convert(jsdumps(doc), {"to": "zipkin"})


def paths(result):
    return [loss["path"] for loss in result["report"]["losses"]]


def test_supported_otlp_zipkin_normalized_otlp_matches_the_independent_golden():
    result = convert(fixture("common.otlp.json"), {"to": "zipkin", "strictLoss": True})
    assert json.loads(stringify(result["output"])) == clone_fixture("common.zipkin.json")
    back = convert(stringify(result["output"]), {"to": "otlp", "strictLoss": True})
    assert back["output"] == clone_fixture("common.otlp.json")


def test_zipkin_reports_links_typed_tags_resource_event_attributes_and_precision():
    text = fixture("zipkin-lossy.otlp.json")
    result = convert(text, {"to": "zipkin"})
    assert json.loads(stringify(result["output"])) == clone_fixture("zipkin-lossy.zipkin.json")
    assert paths(result) == [
        "$.resourceSpans[0].resource.attributes[1]",
        f"{P}.attributes[1]", f"{P}.attributes[2]", f"{P}.attributes[3]",
        f"{P}.endTimeUnixNano", f"{P}.events[0].attributes[0]",
        f"{P}.events[0].timeUnixNano", f"{P}.startTimeUnixNano", f'{P}["links"]',
    ]
    assert all(isinstance(loss["reason"], str) and loss["reason"] for loss in result["report"]["losses"])
    assert "private-value" not in jsdumps(result["report"])
    with pytest.raises(StrictLossError) as info:
        convert(text, {"to": "zipkin", "strictLoss": True})
    assert info.value.code == "STRICT_LOSS"
    assert info.value.report == result["report"]
    assert getattr(info.value, "output", None) is None
    s = span(convert(stringify(result["output"]), {"to": "otlp", "strictLoss": True})["output"])
    assert s["startTimeUnixNano"] == "1700000000000000000"
    assert s["endTimeUnixNano"] == "1700000000000001000"
    assert s["events"][0]["timeUnixNano"] == "1700000000000001000"


def test_zipkin_output_retains_exact_large_numeric_timestamp_duration_and_annotation_tokens():
    doc = simple_otlp()
    s = span(doc)
    s["startTimeUnixNano"] = "9007199254740993000"
    s["endTimeUnixNano"] = "18014398509481986000"
    s["events"] = [{"timeUnixNano": "18446744073709551000", "name": "last"}]
    result = convert(jsdumps(doc), {"to": "zipkin", "strictLoss": True})
    wire = stringify(result["output"])
    assert re.search(r'"timestamp": 9007199254740993,', wire)
    assert re.search(r'"duration": 9007199254740993,', wire)
    assert re.search(r'"timestamp": 18446744073709551,', wire)
    assert int(parse_json(wire)[0]["timestamp"]) == 9007199254740993
    back = convert(wire, {"to": "otlp", "strictLoss": True})
    assert span(back["output"])["startTimeUnixNano"] == s["startTimeUnixNano"]
    assert span(back["output"])["endTimeUnixNano"] == s["endTimeUnixNano"]
    assert span(back["output"])["events"][0]["timeUnixNano"] == s["events"][0]["timeUnixNano"]


@pytest.mark.parametrize("nanos", [0, 1, 999, 1000, 1001])
def test_zipkin_duration_obeys_the_upstream_minimum_of_1us_and_reports_rounding(nanos):
    doc = simple_otlp()
    s = span(doc)
    s["startTimeUnixNano"] = "0"
    s["endTimeUnixNano"] = str(nanos)
    result = zipkin(doc)
    assert result["output"][0]["duration"] == 1
    assert paths(result) == ([] if nanos == 1000 else [f"{P}.endTimeUnixNano"])
    if nanos != 1000:
        with pytest.raises(StrictLossError, match="strict-loss"):
            convert(jsdumps(doc), {"to": "zipkin", "strictLoss": True})


def test_zipkin_minimum_duration_refuses_a_uint64_boundary_overflow_on_reimport():
    doc = simple_otlp()
    s = span(doc)
    s["startTimeUnixNano"] = s["endTimeUnixNano"] = "18446744073709551615"
    with pytest.raises(Exception, match="endTimeUnixNano: Zipkin minimum duration would exceed uint64"):
        zipkin(doc)


def test_annotation_collisions_after_precision_or_attribute_loss_collapse_with_duplicate_path():
    doc = simple_otlp()
    span(doc)["events"] = [
        {"name": "same", "timeUnixNano": "1700000000000001000"},
        {"name": "same", "timeUnixNano": "1700000000000001999",
         "attributes": [{"key": "x", "value": {"stringValue": "private-value"}}]},
        {"name": "different", "timeUnixNano": "1700000000000001000"},
    ]
    result = zipkin(doc)
    assert result["output"][0]["annotations"] == [{"timestamp": 1700000000000001, "value": "different"},
                                                  {"timestamp": 1700000000000001, "value": "same"}]
    assert paths(result) == [f"{P}.events[1]", f"{P}.events[1].attributes[0]", f"{P}.events[1].timeUnixNano"]
    assert "duplicate annotation" in result["report"]["losses"][0]["reason"]


def test_identical_annotations_are_also_explicitly_collapsed():
    doc = simple_otlp()
    s = span(doc)
    s["events"] = [{"name": "same", "timeUnixNano": s["startTimeUnixNano"]} for _ in range(2)]
    result = zipkin(doc)
    assert len(result["output"][0]["annotations"]) == 1
    assert paths(result) == [f"{P}.events[1]"]


def test_zipkin_kind_maps_server_client_producer_consumer_and_reports_internal():
    for kind, expected in ((0, None), (1, None), (2, "SERVER"), (3, "CLIENT"), (4, "PRODUCER"), (5, "CONSUMER")):
        doc = simple_otlp()
        span(doc)["kind"] = kind
        result = zipkin(doc)
        assert result["output"][0].get("kind") == expected
        assert paths(result) == ([f"{P}.kind"] if kind == 1 else [])


def test_status_losses_reflect_zipkin_error_tag_inference_without_inventing_tags():
    for code in (0, 1, 2):
        for has_error in (False, True):
            doc = simple_otlp()
            s = span(doc)
            s["status"] = {"code": code, "message": "private-value"}
            s["attributes"] = [{"key": "error", "value": {"stringValue": ""}}] if has_error else []
            result = zipkin(doc)
            assert result["output"][0]["tags"] == ({"error": ""} if has_error else {})
            assert paths(result) == [*([] if code == (2 if has_error else 0) else [f"{P}.status.code"]),
                                     f"{P}.status.message"]
    doc = simple_otlp()
    span(doc)["attributes"] = [{"key": "error", "value": {"stringValue": "timeout"}}]
    # An absent status defaults to UNSET: anchor the inferred-status change to the present tag.
    result = zipkin(doc)
    assert paths(result) == [f"{P}.attributes[0]"]
    assert "status" in result["report"]["losses"][0]["reason"]


def test_jaeger_losses_use_original_convention_tag_process_tag_and_log_field_paths():
    doc = clone_fixture("common.jaeger.json")
    s = doc["data"][0]["spans"][0]
    s["tags"] = [{"key": "span.kind", "type": "string", "value": "internal"},
                 {"key": "otel.status_code", "type": "string", "value": "OK"},
                 {"key": "otel.status_description", "type": "string", "value": "private-value"}]
    doc["data"][0]["processes"]["p2"]["tags"].append({"key": "host.name", "type": "string", "value": "private-value"})
    s["logs"][0]["fields"].append({"key": "bytes", "type": "int64", "value": 42})
    result = convert(jsdumps(doc), {"to": "zipkin"})
    assert paths(result) == ['$.data[0].processes["p2"].tags[0]', "$.data[0].spans[0].logs[0].fields[1]",
                             "$.data[0].spans[0].tags[0]", "$.data[0].spans[0].tags[1]", "$.data[0].spans[0].tags[2]"]


def test_jaeger_inferred_error_and_nonstring_error_tag_each_report_their_loss():
    doc = clone_fixture("common.jaeger.json")
    doc["data"][0]["spans"][0]["tags"] = [{"key": "error", "type": "bool", "value": True}]
    result = convert(jsdumps(doc), {"to": "zipkin"})
    assert paths(result) == ["$.data[0].spans[0].tags[0]"] * 2
    assert result["output"][0]["tags"] == {}


def test_zipkin_output_preserves_absent_empty_service_short_uppercase_ids_and_special_keys():
    for service in (None, ""):
        doc = simple_otlp()
        s = span(doc)
        doc["resourceSpans"][0]["resource"]["attributes"] = (
            [] if service is None else [{"key": "service.name", "value": {"stringValue": service}}])
        s["traceId"] = "ABCDEF0123456789ABCDEF0123456789"
        s["attributes"] = [{"key": key, "value": {"stringValue": "世界"}}
                           for key in ("__proto__", "constructor", "event", "otel.status_code", "span.kind", 'x"\n]')]
        result = convert(jsdumps(doc), {"to": "zipkin", "strictLoss": True})
        assert result["output"][0].get("localEndpoint") == (None if service is None else {"serviceName": ""})
        assert result["output"][0]["traceId"] == s["traceId"].lower()
        back = convert(stringify(result["output"]), {"to": "otlp", "strictLoss": True})
        assert back["output"] == convert(jsdumps(doc), {"to": "otlp", "strictLoss": True})["output"]
    short = clone_fixture("common.zipkin.json")
    short[0]["traceId"] = "ABCDEF0123456789"
    assert zipkin(short)["output"][0]["traceId"] == "0000000000000000abcdef0123456789"


def test_zipkin_output_is_deterministic_across_input_ordering():
    doc = clone_fixture("common.otlp.json")
    a = zipkin(doc)
    doc["resourceSpans"].reverse()
    for r in doc["resourceSpans"]:
        for s in r["scopeSpans"][0]["spans"]:
            s["attributes"].reverse()
            s["events"].reverse()
    b = zipkin(doc)
    assert stringify(a["output"]) == stringify(b["output"])
