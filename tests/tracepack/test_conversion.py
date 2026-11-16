# SPDX-License-Identifier: Apache-2.0
import copy
import json
import re

import pytest

from otelwrench.tracepack import StrictLossError, convert, stringify

from .support import clone_fixture, fixture, jsdumps, simple_otlp, span


@pytest.mark.parametrize("source", ["zipkin", "otlp", "jaeger"])
@pytest.mark.parametrize("target", ["otlp", "jaeger", "zipkin"])
def test_golden_cross_service_parent_error_status_and_events(source, target):
    result = convert(fixture(f"common.{source}.json"), {"to": target, "from": source, "strictLoss": True})
    assert json.loads(stringify(result["output"])) == clone_fixture(f"common.{target}.json")
    assert result["report"]["spanCount"] == 2
    assert result["report"]["losses"] == []


def test_supported_otlp_jaeger_otlp_semantic_round_trip_matches_golden():
    result = convert(fixture("common.otlp.json"), {"to": "jaeger", "strictLoss": True})
    back = convert(stringify(result["output"]), {"to": "otlp", "strictLoss": True})
    assert back["output"] == clone_fixture("common.otlp.json")


def test_primitive_tags_resource_tags_typed_event_attrs_all_status_kind_values_round_trip():
    for code in range(3):
        for kind in range(6):
            doc = simple_otlp()
            s = span(doc)
            doc["resourceSpans"][0]["resource"]["attributes"].append(
                {"key": "host.name", "value": {"stringValue": "synthetic"}})
            s["status"] = {"code": code, "message": "status detail" if code else ""}
            s["kind"] = kind
            s["attributes"] = [
                {"key": "text", "value": {"stringValue": 'hi\n"世界'}},
                {"key": "yes", "value": {"boolValue": True}},
                {"key": "no", "value": {"boolValue": False}},
                {"key": "huge", "value": {"intValue": "9223372036854775807"}},
                {"key": "negative", "value": {"intValue": "-9223372036854775808"}},
                {"key": "ratio", "value": {"doubleValue": 0.125}},
                {"key": "empty", "value": {"stringValue": ""}},
            ]
            s["events"] = [{"timeUnixNano": s["startTimeUnixNano"], "name": "received", "attributes": s["attributes"]}]
            canonical = convert(jsdumps(doc), {"to": "otlp", "strictLoss": True})
            jaeger = convert(jsdumps(doc), {"to": "jaeger", "strictLoss": True})
            back = convert(stringify(jaeger["output"]), {"to": "otlp", "strictLoss": True})
            assert back["output"] == canonical["output"]
            assert re.search(r'"value": 9223372036854775807', stringify(jaeger["output"]))


def _web_span(output):
    return next(r for r in output["resourceSpans"]
                if r["resource"]["attributes"][0]["value"]["stringValue"] == "web")["scopeSpans"][0]["spans"][0]


def test_jaeger_error_true_convention_infers_error_and_retains_original_tag():
    doc = clone_fixture("common.jaeger.json")
    doc["data"][0]["spans"][0]["tags"] = [{"key": "error", "type": "bool", "value": True}]
    result = convert(jsdumps(doc), {"to": "otlp", "strictLoss": True})
    s = _web_span(result["output"])
    assert s["status"]["code"] == 2
    assert s["attributes"] == [{"key": "error", "value": {"boolValue": True}}]


def test_jaeger_log_without_event_becomes_unnamed_otlp_event_and_retains_typed_fields():
    doc = clone_fixture("common.jaeger.json")
    doc["data"][0]["spans"][0]["logs"][0]["fields"] = [{"key": "bytes", "type": "int64", "value": 42}]
    s = _web_span(convert(jsdumps(doc), {"to": "otlp"})["output"])
    assert s["events"][0]["name"] == ""
    assert s["events"][0]["attributes"] == [{"key": "bytes", "value": {"intValue": "42"}}]


@pytest.mark.parametrize("text,source", [("[]", "zipkin"), ('{"resourceSpans":[]}', "otlp"), ('{"data":[]}', "jaeger")])
@pytest.mark.parametrize("target", ["otlp", "jaeger", "zipkin"])
def test_empty_documents(text, source, target):
    result = convert(text, {"to": target, "strictLoss": True})
    assert result["report"]["from"] == source
    assert result["report"]["spanCount"] == 0
    expected = {"resourceSpans": []} if target == "otlp" else {"data": []} if target == "jaeger" else []
    assert result["output"] == expected


def test_identical_duplicate_is_reported_and_conflicting_duplicate_rejected():
    doc = simple_otlp()
    spans = doc["resourceSpans"][0]["scopeSpans"][0]["spans"]
    spans.append(copy.deepcopy(spans[0]))
    result = convert(jsdumps(doc), {"to": "otlp"})
    assert result["report"]["spanCount"] == 1
    assert "duplicate" in result["report"]["losses"][0]["reason"]
    with pytest.raises(StrictLossError, match="strict-loss"):
        convert(jsdumps(doc), {"to": "otlp", "strictLoss": True})
    spans[1]["name"] = "conflict"
    with pytest.raises(Exception, match="conflicting duplicate"):
        convert(jsdumps(doc), {"to": "otlp"})


def test_deterministic_ordering_ignores_input_order():
    doc = clone_fixture("common.otlp.json")
    for r in doc["resourceSpans"]:
        s = r["scopeSpans"][0]["spans"][0]
        s["attributes"] += [{"key": "z", "value": {"stringValue": "last"}}, {"key": "a", "value": {"stringValue": "first"}}]
        s["events"] += [{"timeUnixNano": s["startTimeUnixNano"], "name": "same", "attributes": [
            {"key": "same", "value": {"stringValue": "same"}}, {"key": "variable", "value": {"stringValue": value}}]}
            for value in ["b", "a"]]
    a = convert(jsdumps(doc), {"to": "jaeger", "strictLoss": True})
    doc["resourceSpans"].reverse()
    for r in doc["resourceSpans"]:
        r["resource"]["attributes"].reverse()
        for s in r["scopeSpans"][0]["spans"]:
            s["attributes"].reverse()
            s["events"].reverse()
            for e in s["events"]:
                e["attributes"].reverse()
    b = convert(jsdumps(doc), {"to": "jaeger", "strictLoss": True})
    assert stringify(a["output"]) == stringify(b["output"])


def test_multiple_traces_and_partial_external_parents_remain_distinct():
    doc = clone_fixture("common.zipkin.json")
    doc.append({**doc[1], "traceId": "1234567890abcdef", "parentId": "3333333333333333"})
    out = convert(jsdumps(doc), {"to": "jaeger", "strictLoss": True})["output"]
    assert len(out["data"]) == 2
    assert out["data"][0]["traceID"] == "00000000000000001234567890abcdef"
    assert out["data"][0]["spans"][0]["references"][0]["spanID"] == "3333333333333333"
