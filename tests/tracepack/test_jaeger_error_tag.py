# SPDX-License-Identifier: Apache-2.0
import copy
import json
import os

import pytest

from otelwrench.tracepack import StrictLossError, convert, stringify

from .support import example, fixture, jsdumps, run, span

SOURCE = example("jaeger-error.json")
GOLDEN = json.loads(fixture("jaeger-error.zipkin.json"))
REPORT = json.loads(fixture("jaeger-error.report.json"))
OPTIONS = {"to": "zipkin", "zipkinErrorTag": True}
SYNTHESIS = REPORT["losses"][1]["reason"]


def source_doc():
    return json.loads(SOURCE)


def jaeger_span(doc):
    return doc["data"][0]["spans"][0]


def mapped(doc):
    return convert(jsdumps(doc), OPTIONS)


def run_cli(doc, flags=()):
    return run(["convert", "-", "--to", "zipkin", "--zipkin-error-tag", *flags], jsdumps(doc))


def collision(path):
    return f"{path}: nonstring error attribute collides with --zipkin-error-tag mapping"


def no_description():
    doc = source_doc()
    del jaeger_span(doc)["tags"][0]
    return doc


def test_jaeger_opt_in_matches_goldens_and_reimport_retains_error():
    for source in ("auto", "jaeger"):
        result = convert(SOURCE, {**OPTIONS, "from": source})
        assert json.loads(stringify(result["output"])) == GOLDEN
        assert result["report"] == REPORT
        back = convert(stringify(result["output"]), {"to": "otlp", "strictLoss": True})["output"]
        assert span(back)["status"] == {"code": 2, "message": ""}
        assert span(back)["attributes"] == [{"key": "error", "value": {"stringValue": ""}},
                                            {"key": "http.method", "value": {"stringValue": "GET"}}]


def test_empty_or_absent_description_reports_only_synthesis_and_strict_loss_rejects_it():
    for doc in (source_doc(), no_description()):
        desc = next((t for t in jaeger_span(doc)["tags"] if t["key"] == "otel.status_description"), None)
        if desc:
            desc["value"] = ""
        result = mapped(doc)
        index = next(i for i, t in enumerate(jaeger_span(doc)["tags"]) if t["key"] == "otel.status_code")
        assert result["report"]["losses"] == [{"path": f"$.data[0].spans[0].tags[{index}]", "reason": SYNTHESIS}]
        with pytest.raises(StrictLossError) as info:
            convert(jsdumps(doc), {**OPTIONS, "strictLoss": True})
        assert info.value.code == "STRICT_LOSS" and info.value.report == result["report"]


def test_source_paths_survive_reordered_tags_traces_and_normalized_span_order():
    doc = source_doc()
    first = copy.deepcopy(jaeger_span(doc))
    first["spanID"] = "2222222222222222"
    first["startTime"] -= 1000
    first["tags"].reverse()
    second_trace = copy.deepcopy(doc["data"][0])
    second_trace["traceID"] = "0000000000000000abcdef0123456789"
    second_trace["spans"][0]["traceID"] = second_trace["traceID"]
    doc["data"][0]["spans"].append(first)
    doc["data"].append(second_trace)
    assert mapped(doc)["report"]["losses"] == [
        *REPORT["losses"],
        {"path": "$.data[0].spans[1].tags[1]", "reason": SYNTHESIS},
        {"path": "$.data[0].spans[1].tags[3]", "reason": REPORT["losses"][0]["reason"]},
        {"path": "$.data[1].spans[0].tags[0]", "reason": REPORT["losses"][0]["reason"]},
        {"path": "$.data[1].spans[0].tags[2]", "reason": SYNTHESIS},
    ]


def test_jaeger_existing_string_error_tags_stay_literal_without_synthesis():
    for value in ("", "false", "private-error-value"):
        doc = no_description()
        jaeger_span(doc)["tags"].append({"key": "error", "type": "string", "value": value})
        result = convert(jsdumps(doc), {**OPTIONS, "strictLoss": True})
        assert result["output"][0]["tags"] == {"error": value, "http.method": "GET"}
        assert result["report"]["losses"] == []
        jaeger_span(doc)["tags"].insert(0, {"key": "otel.status_description", "type": "string", "value": "private-message"})
        assert mapped(doc)["report"]["losses"] == [REPORT["losses"][0]]
        assert "private-" not in jsdumps(mapped(doc)["report"])


@pytest.mark.parametrize("type_,value", [("bool", False), ("bool", True), ("int64", "7"), ("float64", 1.5),
                                         ("binary", "eA==")])
def test_jaeger_error_rejects_collision_at_original_tag_path(type_, value):
    doc = source_doc()
    jaeger_span(doc)["tags"].append({"key": "error", "type": type_, "value": value})
    with pytest.raises(Exception) as info:
        mapped(doc)
    assert str(info.value) == collision("$.data[0].spans[0].tags[4]")


def test_binary_error_collisions_in_duplicates_fail_before_collapse_regardless_of_order():
    for reverse in (False, True):
        doc = no_description()
        duplicate = copy.deepcopy(jaeger_span(doc))
        duplicate["tags"].append({"key": "error", "type": "binary", "value": "eA=="})
        doc["data"][0]["spans"].append(duplicate)
        if reverse:
            doc["data"][0]["spans"].reverse()
        with pytest.raises(Exception) as info:
            mapped(doc)
        assert str(info.value) == collision(f"$.data[0].spans[{0 if reverse else 1}].tags[3]")


def test_jaeger_inferred_boolean_error_rejects_rather_than_overwriting():
    doc = no_description()
    jaeger_span(doc)["tags"] = [{"key": "error", "type": "bool", "value": True}]
    with pytest.raises(Exception) as info:
        mapped(doc)
    assert str(info.value) == collision("$.data[0].spans[0].tags[0]")


def test_unset_ok_and_absent_status_keep_prior_behavior_and_option_defaults_to_false():
    for code in (None, "UNSET", "OK"):
        tags = [None, {"type": "string", "value": ""}, {"type": "bool", "value": False},
                {"type": "binary", "value": "eA=="}, *([{"type": "bool", "value": True}] if code else [])]
        for tag in tags:
            doc = source_doc()
            s = jaeger_span(doc)
            s["tags"] = [t for t in s["tags"] if t["key"] != "otel.status_code"]
            if code:
                s["tags"].append({"key": "otel.status_code", "type": "string", "value": code})
            if tag:
                s["tags"].append({"key": "error", **tag})
            assert mapped(doc) == convert(jsdumps(doc), {"to": "zipkin"})
    normal = convert(SOURCE, {"to": "zipkin"})
    assert normal == convert(SOURCE, {**OPTIONS, "zipkinErrorTag": False})
    assert normal["output"][0]["tags"] == {"http.method": "GET"}
    assert [loss["path"] for loss in normal["report"]["losses"]] == [loss["path"] for loss in REPORT["losses"]]
    assert "status code cannot be preserved" in normal["report"]["losses"][1]["reason"]


def test_resource_and_log_error_tags_remain_independent_scopes():
    doc = source_doc()
    doc["data"][0]["processes"]["p1"]["tags"] = [{"key": "error", "type": "bool", "value": True}]
    jaeger_span(doc)["logs"] = [{"timestamp": 1700000000000000, "fields": [
        {"key": "event", "type": "string", "value": "failed"}, {"key": "error", "type": "binary", "value": "eA=="}]}]
    result = mapped(doc)
    assert result["output"][0]["tags"] == GOLDEN[0]["tags"]
    assert result["report"]["losses"] == [
        {"path": '$.data[0].processes["p1"].tags[0]', "reason": "resource attribute dropped; Zipkin only represents service.name"},
        {"path": "$.data[0].spans[0].logs[0].fields[1]", "reason": "binary attribute dropped"},
        *REPORT["losses"],
    ]


def test_jaeger_cli_golden_strict_loss_dry_run_and_collisions_preserve_output_contracts(tmp_path):
    output = tmp_path / "out.json"
    normal = run_cli(source_doc())
    assert normal.status == 0, normal.stderr
    assert json.loads(normal.stdout) == GOLDEN
    assert json.loads(normal.stderr) == REPORT
    for exists in (False, True):
        if exists:
            output.write_text("sentinel")
        cases = [(source_doc(), ["--strict-loss"], 2), (source_doc(), ["--dry-run"], 0),
                 (source_doc(), ["--strict-loss", "--dry-run"], 2)]
        for type_, value in (("bool", True), ("int64", "7"), ("float64", 1.5), ("binary", "eA==")):
            doc = source_doc()
            jaeger_span(doc)["tags"].append({"key": "error", "type": type_, "value": value})
            cases.append((doc, [], 1))
        duplicate = no_description()
        copy_span = copy.deepcopy(jaeger_span(duplicate))
        copy_span["tags"].append({"key": "error", "type": "binary", "value": "eA=="})
        duplicate["data"][0]["spans"].append(copy_span)
        cases.append((duplicate, [], 1))
        for doc, flags, status in cases:
            result = run_cli(doc, ["--output", output, *flags])
            assert result.status == status, result.stderr
            assert result.stdout == ""
            message = json.loads(result.stderr)
            if status == 1:
                assert "nonstring error attribute collides" in message["error"]
            else:
                assert (message["report"] if status == 2 else message) == REPORT
            assert os.listdir(tmp_path) == (["out.json"] if exists else [])
            if exists:
                assert output.read_text() == "sentinel"
