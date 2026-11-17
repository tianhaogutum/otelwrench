import json

import pytest

from otelwrench.privacy.inputs import (
    ReplayError,
    load_yaml,
    sample_messages,
    selected_pipeline,
    strict_json,
)


def test_selected_pipeline_preserves_actual_order():
    cfg = {
        "processors": {"redaction/a": {"summary": "silent"}, "attributes/b": {}},
        "service": {"pipelines": {"traces/send": {"processors": ["attributes/b", "redaction/a"]}}},
    }
    signal, chain = selected_pipeline(cfg, "traces/send")
    assert signal == "traces"
    assert [name for name, _ in chain] == ["attributes/b", "redaction/a"]


@pytest.mark.parametrize(
    "name,body",
    [
        ("routing", {}),
        ("redaction", {"blocked_values": ["${env:PRIVATE_PATTERN}"]}),
    ],
)
def test_rejects_unsupported_or_unresolved_processor(name, body):
    cfg = {"processors": {name: body}, "service": {"pipelines": {"traces": {"processors": [name]}}}}
    with pytest.raises(ReplayError):
        selected_pipeline(cfg, "traces")


def test_duplicate_yaml_is_not_silently_overwritten(local_tmp):
    path = local_tmp / "duplicate.yaml"
    path.write_text("processors:\n  redaction: {}\n  redaction: {allow_all_keys: true}\n")
    with pytest.raises(ReplayError, match="duplicate"):
        load_yaml(path)


@pytest.mark.parametrize("body", [False, 0, "", []])
def test_falsy_nonmapping_processor_is_not_silently_replaced(body):
    cfg = {
        "processors": {"redaction": body},
        "service": {"pipelines": {"traces": {"processors": ["redaction"]}}},
    }
    with pytest.raises(ReplayError, match="mapping"):
        selected_pipeline(cfg, "traces")


@pytest.mark.parametrize(
    "text",
    [
        "cycle: &cycle {again: *cycle}",
        "a: &a [x,x,x,x,x,x,x,x,x,x]\nb: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a,*a]\n"
        "c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b,*b]\nd: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c,*c]\n"
        "e: &e [*d,*d,*d,*d,*d,*d,*d,*d,*d,*d]\nf: [*e,*e,*e,*e,*e,*e,*e,*e,*e,*e]",
    ],
)
def test_recursive_and_expanding_aliases_fail_bounded(local_tmp, text):
    path = local_tmp / "aliases.yaml"
    path.write_text(text)
    with pytest.raises(ReplayError, match="aliases|structure"):
        load_yaml(path)


@pytest.mark.parametrize("data", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_strict_json(data):
    with pytest.raises(ReplayError):
        strict_json(data)


def test_real_jsonl_request_boundaries(local_tmp):
    path = local_tmp / "file-exporter.jsonl"
    path.write_text('{"resourceSpans":[]}\n{"resourceSpans":[]}\n')
    messages, kind = sample_messages(path, "traces")
    assert kind == "application/json"
    assert len(messages) == 2
    assert all(json.loads(message) == {"resourceSpans": []} for message in messages)


@pytest.mark.parametrize(
    "data", ["", "garbage", '{"resourceLogs":[]}', '{"resourceSpans":[],"resourceLogs":[]}']
)
def test_rejects_bad_samples(local_tmp, data):
    path = local_tmp / "bad.json"
    path.write_text(data)
    with pytest.raises(ReplayError):
        sample_messages(path, "traces")
