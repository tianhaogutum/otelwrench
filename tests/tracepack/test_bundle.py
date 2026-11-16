# SPDX-License-Identifier: Apache-2.0
import json
import os

import pytest

from otelwrench.tracepack import StrictLossError, canonical_json, create_bundle, verify_bundle

from .support import (EXAMPLES, b64, clone_fixture, fixture, jsdumps, run, same_float, sha, simple_otlp, span, unb64,
                      wire)


def verify(bundle):
    return verify_bundle(wire(bundle))


def reverse_keys(value):
    if isinstance(value, list):
        return [reverse_keys(v) for v in value]
    if isinstance(value, dict):
        return {k: reverse_keys(v) for k, v in reversed(list(value.items()))}
    return value


def rehash(bundle, member):
    bundle["sha256"][member] = sha(unb64(bundle["source"]["data"]) if member == "source"
                                   else canonical_json(bundle[member]))


@pytest.mark.parametrize("source", ["otlp", "jaeger", "zipkin"])
def test_bundle_original_bytes_options_normalized_golden_and_offline_replay(source):
    text = fixture(f"common.{source}.json")
    bundle = create_bundle(text)
    assert bundle["format"] == "tracepack-fixture" and bundle["version"] == 1
    assert bundle["converter"] == {"name": "tracepack", "version": "0.1.0", "normalizationVersion": 1}
    assert unb64(bundle["source"]["data"]).decode("utf-8") == text
    assert bundle["sha256"]["source"] == sha(text)
    assert bundle["options"] == {"from": source, "to": "otlp", "strictLoss": False}
    assert bundle["normalized"] == clone_fixture("common.otlp.json")
    assert bundle["lossReport"] == {"from": source, "to": "otlp", "spanCount": 2, "losses": []}
    assert verify(bundle) == {"valid": True, "format": "tracepack-fixture", "version": 1, "sourceSha256": sha(text),
                              "spanCount": 2, "lossCount": 0, "guarantee": "reproducibility-only"}
    assert wire(create_bundle(text)) == wire(bundle)
    reordered = create_bundle(jsdumps(reverse_keys(json.loads(text))))
    assert canonical_json(reordered["normalized"]) == canonical_json(bundle["normalized"])
    assert reordered["sha256"]["normalized"] == bundle["sha256"]["normalized"]
    assert reordered["lossReport"] == bundle["lossReport"]
    assert reordered["sha256"]["source"] != bundle["sha256"]["source"]
    assert verify(reverse_keys(bundle)) == verify(bundle)


def test_bundle_keeps_original_lossy_input_requires_accurate_report_and_honors_strict_loss():
    doc = simple_otlp()
    span(doc)["links"] = [{"traceId": "f" * 32}]
    text = jsdumps(doc)
    bundle = create_bundle(text, {"from": "otlp"})
    assert bundle["lossReport"]["losses"] == [
        {"path": '$.resourceSpans[0].scopeSpans[0].spans[0]["links"]', "reason": "unsupported field dropped"}]
    assert verify(bundle)["lossCount"] == 1
    with pytest.raises(StrictLossError) as info:
        create_bundle(text, {"strictLoss": True})
    assert info.value.code == "STRICT_LOSS"
    bundle["lossReport"]["losses"] = []
    rehash(bundle, "lossReport")
    with pytest.raises(Exception, match="lossReport.*replay|replay.*lossReport"):
        verify(bundle)


@pytest.mark.parametrize("member", ["source", "options", "normalized", "lossReport"])
def test_tampered_member_fails_its_member_hash(member):
    bundle = create_bundle(fixture("common.zipkin.json"))
    if member == "source":
        bundle["source"]["data"] = b64(b"[]")
    if member == "options":
        bundle["options"]["strictLoss"] = True
    if member == "normalized":
        span(bundle["normalized"])["name"] = "tampered"
    if member == "lossReport":
        bundle["lossReport"]["spanCount"] = 3
    with pytest.raises(Exception, match=f"{member}.*SHA-256"):
        verify(bundle)


def test_replay_rejects_rehashed_normalized_content_options_and_source_edits():
    for member in ("source", "options", "normalized"):
        bundle = create_bundle(fixture("common.zipkin.json"))
        if member == "source":
            bundle["source"]["data"] = b64(b"[]")
        if member == "options":
            bundle["options"]["from"] = "otlp"
        if member == "normalized":
            span(bundle["normalized"])["name"] = "tampered"
        rehash(bundle, member)
        with pytest.raises(Exception, match="replay|resourceSpans"):
            verify(bundle)


def _set(path, value):
    def mutate(b):
        target = b
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return mutate


def _delete(path):
    def mutate(b):
        target = b
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
    return mutate


def _append_newline(b):
    b["source"]["data"] += "\n"


def test_schema_versions_omitted_members_duplicate_keys_and_invalid_encodings_fail():
    mutations = [
        (_set(["version"], 2), "version"),
        (_set(["converter", "version"], "0.0.0"), "converter"),
        (_set(["converter", "normalizationVersion"], 2), "normalizationVersion"),
        (_delete(["lossReport"]), "lossReport"),
        (_delete(["sha256", "lossReport"]), "lossReport"),
        (_append_newline, "base64"),
        (_set(["source", "encoding"], "utf8"), "encoding"),
        (_set(["options", "to"], "jaeger"), "options"),
        (_set(["options", "strictLoss"], "false"), "strictLoss"),
        (_set(["unexpected"], True), "unexpected"),
        (_set(["sha256", "normalized"], "z" * 64), "SHA-256"),
    ]
    for mutate, expected in mutations:
        bundle = create_bundle(fixture("common.zipkin.json"))
        mutate(bundle)
        with pytest.raises(Exception, match=expected):
            verify(bundle)
    with pytest.raises(Exception, match="Duplicate object key"):
        verify_bundle('{"version":1,"version":1}')


def test_source_utf8_validation_bom_and_exact_unsafe_numeric_tokens_survive_the_bundle():
    with pytest.raises(Exception, match="encoded data|UTF-8"):
        create_bundle(bytes([0xFF]))
    # Keep the unsafe integer token out of the platform JSON parser entirely.
    data = clone_fixture("common.zipkin.json")[:1]
    raw = ("﻿" + jsdumps(data).replace("1700000000000000", "9007199254740993", 1)).encode()
    bundle = create_bundle(raw)
    assert bundle["sha256"]["source"] == sha(raw)
    assert unb64(bundle["source"]["data"]) == raw
    assert span(bundle["normalized"])["startTimeUnixNano"] == "9007199254740993000"
    assert verify(bundle)["valid"] is True


def test_normalized_finite_doubles_and_negative_zero_remain_replayable():
    for token in ["-0", "-0.0", "1e20", "1.7976931348623157e308", "5e-324"]:
        doc = simple_otlp()
        span(doc)["attributes"] = [{"key": "value", "value": {"doubleValue": "TOKEN"}}]
        bundle = create_bundle(jsdumps(doc).replace('"TOKEN"', token))
        assert verify(bundle)["valid"] is True, token
        assert same_float(span(bundle["normalized"])["attributes"][0]["value"]["doubleValue"], float(token))


def test_an_internally_consistent_regenerated_bundle_can_pass():
    doc = clone_fixture("common.zipkin.json")
    doc[0]["name"] = "different source"
    assert verify(create_bundle(jsdumps(doc)))["guarantee"] == "reproducibility-only"


def test_packaged_example_bundle_verifies_and_exactly_reproduces_the_documented_input():
    source = (EXAMPLES / "zipkin.json").read_bytes()
    example = (EXAMPLES / "zipkin.tracepack.json").read_text(encoding="utf-8")
    assert verify_bundle(example)["valid"] is True
    assert example == wire(create_bundle(source, {"strictLoss": True}))


def test_cli_bundle_verify_from_stdin_atomic_output_strict_loss_and_verify_failure_channels(tmp_path):
    path, source = tmp_path / "bundle.json", fixture("common.zipkin.json")
    created = run(["bundle", "-", "--from", "zipkin", "--strict-loss", "--output", path], source)
    assert created.status == 0, created.stderr
    assert created.stdout == ""
    assert json.loads(created.stderr) == {"from": "zipkin", "to": "otlp", "spanCount": 2, "losses": []}
    bundle = json.loads(path.read_text(encoding="utf-8"))
    assert bundle["normalized"] == clone_fixture("common.otlp.json")
    checked = run(["verify", path])
    assert checked.status == 0, checked.stderr
    assert json.loads(checked.stdout)["guarantee"] == "reproducibility-only" and checked.stderr == ""
    assert run(["verify", "-"], path.read_bytes()).status == 0
    lossy = simple_otlp()
    span(lossy)["links"] = []
    refused = run(["bundle", "-", "--strict-loss", "--output", path], jsdumps(lossy))
    assert refused.status == 2, refused.stderr
    assert refused.stdout == ""
    assert path.read_text(encoding="utf-8") == wire(bundle) and os.listdir(tmp_path) == ["bundle.json"]
    del bundle["lossReport"]
    path.write_text(jsdumps(bundle), encoding="utf-8")
    invalid = run(["verify", path])
    assert invalid.status == 1 and invalid.stdout == ""
    assert "lossReport" in json.loads(invalid.stderr)["error"]
    for args in (["verify", path, "--output", path], ["verify", path, "--from", "zipkin"], ["bundle", "-", "--to", "jaeger"]):
        result = run(args, source)
        assert result.status == 1 and result.stdout == ""
