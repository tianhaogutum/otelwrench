# SPDX-License-Identifier: Apache-2.0
import copy
import json
import os
import re

import pytest

from otelwrench.tracepack import StrictLossError, convert, stringify

from .support import fixture, jsdumps, run, simple_otlp, span

P = "$.resourceSpans[0].scopeSpans[0].spans[0]"
SYNTHESIS = {"path": f"{P}.status.code",
             "reason": "string error tag synthesized from ERROR status; re-import adds an error attribute"}
MESSAGE_LOSS = {"path": f"{P}.status.message", "reason": "status message dropped; Zipkin has no separate status message"}
OPTIONS = {"to": "zipkin", "zipkinErrorTag": True}
_ABSENT = object()


def error_input(message=""):
    doc = simple_otlp()
    span(doc)["status"] = {"code": 2, "message": message}
    return doc


def mapped(doc):
    return convert(jsdumps(doc), OPTIONS)


def run_cli(doc, flags=()):
    return run(["convert", "-", "--to", "zipkin", "--zipkin-error-tag", *flags], jsdumps(doc))


def test_opt_in_error_synthesis_matches_explicit_wire_contract():
    result = mapped(error_input())
    assert json.loads(stringify(result["output"])) == [{
        "traceId": "abcdef0123456789abcdef0123456789", "id": "1111111111111111", "name": "request",
        "timestamp": 1700000000000000, "duration": 1000, "tags": {"error": ""}, "annotations": [],
        "localEndpoint": {"serviceName": "web"}}]
    assert result["report"] == {"from": "otlp", "to": "zipkin", "spanCount": 1, "losses": [SYNTHESIS]}
    back = convert(stringify(result["output"]), {"to": "otlp", "strictLoss": True})["output"]
    assert span(back)["status"] == {"code": 2, "message": ""}
    assert span(back)["attributes"] == [{"key": "error", "value": {"stringValue": ""}}]


def test_synthesis_and_dropped_status_message_are_separate_value_free_entries():
    result = mapped(error_input("private-status-message"))
    assert result["output"][0]["tags"] == {"error": ""}
    assert result["report"]["losses"] == [SYNTHESIS, MESSAGE_LOSS]
    assert "private-status-message" not in jsdumps(result["report"])
    with pytest.raises(StrictLossError) as info:
        convert(jsdumps(error_input("private-status-message")), {**OPTIONS, "strictLoss": True})
    assert info.value.code == "STRICT_LOSS" and info.value.report == result["report"]


def test_existing_string_error_tags_including_empty_strings_are_retained():
    for value in ("", "private-error-value", "false"):
        doc = error_input()
        span(doc)["attributes"] = [{"key": "error", "value": {"stringValue": value}}]
        result = convert(jsdumps(doc), {**OPTIONS, "strictLoss": True})
        assert result["output"][0]["tags"] == {"error": value}
        assert result["report"]["losses"] == []
        span(doc)["status"]["message"] = "private-status-message"
        assert mapped(doc)["report"]["losses"] == [MESSAGE_LOSS]


@pytest.mark.parametrize("value", [{"boolValue": False}, {"boolValue": True}, {"intValue": "7"}, {"doubleValue": 1.5},
                                   {"arrayValue": {"values": []}}, {"kvlistValue": {"values": []}},
                                   {"bytesValue": "eA=="}], ids=lambda v: next(iter(v)))
def test_error_synthesis_rejects_collision_at_its_source_path(value):
    doc = error_input()
    span(doc)["attributes"] = [{"key": "error", "value": value}]
    with pytest.raises(Exception) as info:
        mapped(doc)
    assert str(info.value) == f"{P}.attributes[0]: nonstring error attribute collides with --zipkin-error-tag mapping"
    assert "private-status-message" not in str(info.value)


def test_dropped_error_collision_cannot_hide_in_a_normalized_duplicate_span():
    doc = error_input()
    duplicate = copy.deepcopy(span(doc))
    duplicate["attributes"] = [{"key": "error", "value": {"bytesValue": "eA=="}}]
    doc["resourceSpans"][0]["scopeSpans"][0]["spans"].append(duplicate)
    with pytest.raises(Exception, match=r"spans\[1\]\.attributes\[0\]: nonstring error attribute collides"):
        mapped(doc)


def test_unset_ok_and_absent_status_keep_existing_mapping_even_with_collisions():
    for status in (_ABSENT, {}, {"code": 0}, {"code": 1}):
        for value in (None, {"stringValue": ""}, {"boolValue": True}, {"bytesValue": "eA=="}):
            doc = simple_otlp()
            if status is not _ABSENT:
                span(doc)["status"] = status
            if value:
                span(doc)["attributes"] = [{"key": "error", "value": value}]
            assert mapped(doc) == convert(jsdumps(doc), {"to": "zipkin"})


def test_mapping_is_off_by_default_and_explicit_false_retains_error_status_loss():
    text = jsdumps(error_input())
    original = convert(text, {"to": "zipkin"})
    assert convert(text, {"to": "zipkin", "zipkinErrorTag": False}) == original
    assert original["output"][0]["tags"] == {}
    assert [loss["path"] for loss in original["report"]["losses"]] == [f"{P}.status.code"]
    assert "status code cannot be preserved" in original["report"]["losses"][0]["reason"]


def test_resource_and_event_error_attributes_are_separate_scopes():
    doc = error_input()
    doc["resourceSpans"][0]["resource"]["attributes"].append({"key": "error", "value": {"boolValue": True}})
    span(doc)["events"] = [{"name": "failure", "timeUnixNano": "1700000000000000000",
                            "attributes": [{"key": "error", "value": {"stringValue": "private-event-value"}}]}]
    span(doc)["attributes"] = [{"key": key, "value": {"stringValue": key}} for key in ("a", "z", "__proto__")]
    first = mapped(doc)
    assert first["output"][0]["tags"]["error"] == ""
    assert [loss["path"] for loss in first["report"]["losses"]] == [
        "$.resourceSpans[0].resource.attributes[1]", f"{P}.events[0].attributes[0]", f"{P}.status.code"]
    assert list(first["output"][0]["tags"]) == ["__proto__", "a", "error", "z"]
    span(doc)["attributes"].reverse()
    assert stringify(mapped(doc)["output"]) == stringify(first["output"])


def test_library_rejects_nonboolean_options_and_mapping_outside_otlp_or_jaeger_to_zipkin():
    for flag in (None, 1, "true"):
        with pytest.raises(Exception, match="zipkinErrorTag: expected boolean"):
            convert(jsdumps(error_input()), {"to": "zipkin", "zipkinErrorTag": flag})
    for target in ("otlp", "jaeger"):
        with pytest.raises(Exception, match="only supported for OTLP or Jaeger to Zipkin"):
            convert(jsdumps(error_input()), {"to": target, "zipkinErrorTag": True})
    with pytest.raises(Exception, match="only supported for OTLP or Jaeger to Zipkin"):
        convert(fixture("common.zipkin.json"), {**OPTIONS, "from": "zipkin"})
    with pytest.raises(Exception, match="only supported for OTLP or Jaeger to Zipkin"):
        convert(fixture("common.zipkin.json"), OPTIONS)


def test_cli_mapping_strict_loss_and_dry_run_honor_reports_and_atomic_output(tmp_path):
    output = tmp_path / "out.json"
    doc = error_input("private-status-message")
    normal = run_cli(doc)
    assert normal.status == 0, normal.stderr
    assert json.loads(normal.stdout)[0]["tags"] == {"error": ""}
    assert json.loads(normal.stderr)["losses"] == [SYNTHESIS, MESSAGE_LOSS]
    missing = run_cli(doc, ["--strict-loss", "--output", output])
    assert missing.status == 2, missing.stderr
    assert missing.stdout == "" and os.listdir(tmp_path) == []
    output.write_text("sentinel")
    for extra in ([], ["--dry-run"]):
        refused = run_cli(doc, ["--strict-loss", "--output", output, *extra])
        assert refused.status == 2, refused.stderr
        assert refused.stdout == ""
        assert json.loads(refused.stderr)["report"] == json.loads(normal.stderr)
        assert output.read_text() == "sentinel" and os.listdir(tmp_path) == ["out.json"]
    dry = run_cli(doc, ["--dry-run", "--output", output])
    assert dry.status == 0, dry.stderr
    assert dry.stdout == ""
    assert json.loads(dry.stderr) == json.loads(normal.stderr)
    assert output.read_text() == "sentinel"
    span(doc)["attributes"] = [{"key": "error", "value": {"boolValue": True}}]
    for flags in ([], ["--strict-loss"], ["--dry-run"]):
        collision = run_cli(doc, ["--output", output, *flags])
        assert collision.status == 1, collision.stderr
        assert collision.stdout == ""
        assert "nonstring error attribute collides" in json.loads(collision.stderr)["error"]
        assert output.read_text() == "sentinel" and os.listdir(tmp_path) == ["out.json"]


def test_cli_rejects_duplicate_flag_zipkin_sources_other_targets_and_bundle_verify_use():
    for argv in (["convert", "-", "--to", "zipkin", "--zipkin-error-tag", "--zipkin-error-tag"],
                 ["convert", "-", "--to", "otlp", "--zipkin-error-tag"],
                 ["convert", "-", "--to", "jaeger", "--zipkin-error-tag"],
                 ["bundle", "-", "--zipkin-error-tag"], ["verify", "-", "--zipkin-error-tag"]):
        result = run(argv, jsdumps(error_input()))
        assert result.status == 1 and result.stdout == ""
        assert re.search("duplicate option|only supported for OTLP or Jaeger to Zipkin|unknown option",
                         json.loads(result.stderr)["error"])
    result = run(["convert", "-", "--to", "zipkin", "--zipkin-error-tag"], fixture("common.zipkin.json"))
    assert result.status == 1 and result.stdout == ""
    assert "only supported for OTLP or Jaeger to Zipkin" in json.loads(result.stderr)["error"]
