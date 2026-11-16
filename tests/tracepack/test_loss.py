# SPDX-License-Identifier: Apache-2.0
import pytest

from otelwrench.tracepack import StrictLossError, convert

from .support import clone_fixture, jsdumps, simple_otlp, span


def test_otlp_unsupported_metadata_and_nonprimitive_tags_have_exact_source_paths():
    doc = simple_otlp()
    rs = doc["resourceSpans"][0]
    ss = rs["scopeSpans"][0]
    s = span(doc)
    doc["newField"] = 1
    rs["schemaUrl"] = "https://example.invalid/resource"
    rs["resource"]["droppedAttributesCount"] = 1
    ss["scope"] = {"name": "synthetic-instrumentation"}
    ss["schemaUrl"] = "https://example.invalid/scope"
    s["traceState"] = "x=y"
    s["flags"] = 1
    s["links"] = [{"traceId": s["traceId"], "spanId": "2222222222222222"}]
    s["droppedAttributesCount"] = 1
    s["droppedEventsCount"] = 2
    s["droppedLinksCount"] = 3
    s["attributes"] = [
        {"key": "a", "value": {"arrayValue": {"values": [{"intValue": "1"}]}}},
        {"key": "b", "value": {"kvlistValue": {"values": [{"key": "x", "value": {"boolValue": True}}]}}},
        {"key": "c", "value": {"bytesValue": "YWJj"}},
    ]
    s["events"] = [{"timeUnixNano": s["startTimeUnixNano"], "name": "event", "droppedAttributesCount": 1, "custom": "x"}]
    result = convert(jsdumps(doc), {"to": "otlp"})
    paths = [loss["path"] for loss in result["report"]["losses"]]
    p = "$.resourceSpans[0].scopeSpans[0].spans[0]"
    assert set(paths) == {
        '$["newField"]', '$.resourceSpans[0]["schemaUrl"]', "$.resourceSpans[0].resource.droppedAttributesCount",
        '$.resourceSpans[0].scopeSpans[0]["scope"]', '$.resourceSpans[0].scopeSpans[0]["schemaUrl"]',
        f'{p}["traceState"]', f'{p}["flags"]', f'{p}["links"]',
        f"{p}.droppedAttributesCount", f"{p}.droppedEventsCount", f"{p}.droppedLinksCount",
        f"{p}.attributes[0].value.arrayValue", f"{p}.attributes[1].value.kvlistValue",
        f"{p}.attributes[2].value.bytesValue",
        f"{p}.events[0].droppedAttributesCount", f'{p}.events[0]["custom"]',
    }
    assert len(paths) == 16
    assert span(result["output"])["attributes"] == []
    with pytest.raises(StrictLossError, match="strict-loss") as info:
        convert(jsdumps(doc), {"to": "otlp", "strictLoss": True})
    assert len(info.value.report["losses"]) == 16


def test_zipkin_endpoint_addresses_remote_endpoint_and_debug_are_dropped():
    doc = clone_fixture("common.zipkin.json")
    doc[0]["localEndpoint"]["ipv4"] = "192.0.2.1"
    doc[0]["localEndpoint"]["port"] = 80
    doc[0]["remoteEndpoint"] = {"serviceName": "backend"}
    doc[0]["debug"] = True
    result = convert(jsdumps(doc), {"to": "otlp"})
    assert [loss["path"] for loss in result["report"]["losses"]] == [
        '$[0].localEndpoint["ipv4"]', '$[0].localEndpoint["port"]', '$[0]["debug"]', '$[0]["remoteEndpoint"]']


def test_jaeger_query_metadata_ui_extras_binary_tag_and_unused_process_are_reported():
    doc = clone_fixture("common.jaeger.json")
    t = doc["data"][0]
    doc["total"] = 1
    doc["limit"] = 20
    doc["offset"] = 0
    doc["errors"] = None
    t["spans"][0]["warnings"] = ["synthetic warning"]
    t["spans"][0]["relativeStartTime"] = 0
    t["spans"][0]["tags"].append({"key": "blob", "type": "binary", "value": "YWJj"})
    t["processes"]["unused"] = {"serviceName": "unused", "tags": []}
    result = convert(jsdumps(doc), {"to": "otlp"})
    losses = result["report"]["losses"]
    assert len(losses) == 7
    assert any(loss["path"].endswith("tags[3]") and "binary" in loss["reason"] for loss in losses)
    assert any(loss["path"] == '$.data[0].processes["unused"]' for loss in losses)
    with pytest.raises(StrictLossError, match="strict-loss"):
        convert(jsdumps(doc), {"to": "otlp", "strictLoss": True})


def test_missing_service_name_only_loses_absence_when_producing_jaeger():
    doc = simple_otlp()
    doc["resourceSpans"][0]["resource"]["attributes"] = []
    otlp = convert(jsdumps(doc), {"to": "otlp", "strictLoss": True})
    assert otlp["report"]["losses"] == []
    jaeger = convert(jsdumps(doc), {"to": "jaeger"})
    assert len(jaeger["report"]["losses"]) == 1
    assert "missing service.name" in jaeger["report"]["losses"][0]["reason"]
    assert jaeger["output"]["data"][0]["processes"]["p1"]["serviceName"] == ""


def test_empty_resource_metadata_and_jaeger_empty_trace_identity_are_not_silently_lost():
    otlp = convert('{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"empty"}}]},'
                   '"scopeSpans":[]}]}', {"to": "otlp"})
    assert len(otlp["report"]["losses"]) == 1
    jaeger = convert('{"data":[{"traceID":"1111111111111111","spans":[],"processes":{}}]}', {"to": "otlp"})
    assert len(jaeger["report"]["losses"]) == 1
    assert jaeger["output"] == {"resourceSpans": []}


def test_loss_paths_safely_escape_arbitrary_attribute_and_field_keys():
    doc = clone_fixture("common.zipkin.json")
    doc[0]['x"\n]'] = 3
    result = convert(jsdumps(doc), {"to": "otlp"})
    assert result["report"]["losses"][0]["path"] == '$[0]["x\\"\\n]"]'
