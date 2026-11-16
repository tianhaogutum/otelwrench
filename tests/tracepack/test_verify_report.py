# SPDX-License-Identifier: Apache-2.0
import json
import os
import re

import pytest

from otelwrench.tracepack import canonical_json, create_bundle

from .support import LOSSY, LOSSY_REPORT, ORIGINAL, fixture, json2, run, sha, snapshot, span, wire

LOSSLESS_REPORT = {"from": "zipkin", "to": "otlp", "spanCount": 1, "losses": []}


def summary(source, loss_count=0, span_count=1):
    return {"valid": True, "format": "tracepack-fixture", "version": 1, "sourceSha256": sha(source),
            "spanCount": span_count, "lossCount": loss_count, "guarantee": "reproducibility-only"}


def setup(tmp_path, source=LOSSY, options=None):
    path, saved = tmp_path / "bundle.json", tmp_path / "source.json"
    bundle = create_bundle(source, options)
    text = wire(bundle)
    path.write_text(text, encoding="utf-8")
    saved.write_bytes(source)
    return path, saved, bundle, text


def success(result, expected):
    assert result.status == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == json2(expected)


def failure(result, code, expected):
    assert result.status == code, result.stderr
    assert result.stdout == ""
    assert result.stderr == json2(expected)


@pytest.mark.parametrize("name,source,expected_report", [("lossy", LOSSY, LOSSY_REPORT),
                                                         ("lossless", ORIGINAL, LOSSLESS_REPORT)])
def test_verify_report_returns_complete_reports_only_on_request(tmp_path, name, source, expected_report):
    path, _, _, text = setup(tmp_path, source)
    before = snapshot(tmp_path)
    for p in (path, "-"):
        stdin = text if p == "-" else None
        success(run(["verify", p, "--report"], stdin),
                {**summary(source, len(expected_report["losses"])), "report": expected_report})
        success(run(["verify", p], stdin), summary(source, len(expected_report["losses"])))
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("fmt", ["otlp", "jaeger", "zipkin"])
def test_report_reflects_the_replay_format_and_span_count(tmp_path, fmt):
    source = fixture(f"common.{fmt}.json").encode()
    path, _, _, _ = setup(tmp_path, source, {"from": fmt, "strictLoss": True})
    success(run(["verify", path, "--report", "--strict-loss"]),
            {**summary(source, 0, 2), "report": {"from": fmt, "to": "otlp", "spanCount": 2, "losses": []}})


def test_report_supports_an_empty_trace_and_zero_span_count(tmp_path):
    source = b"[]"
    path, _, _, _ = setup(tmp_path, source)
    success(run(["verify", path, "--report"]),
            {**summary(source, 0, 0), "report": {"from": "zipkin", "to": "otlp", "spanCount": 0, "losses": []}})


def test_report_composes_with_exact_source_and_strict_policy_in_either_order(tmp_path):
    for source in (ORIGINAL, LOSSY):
        directory = tmp_path / ("lossy" if source == LOSSY else "lossless")
        directory.mkdir()
        path, saved, _, text = setup(directory, source)
        before = snapshot(directory)
        expected_report = LOSSY_REPORT if source == LOSSY else LOSSLESS_REPORT
        for p in (path, "-"):
            stdin = text if p == "-" else None
            success(run(["verify", p, "--report", "--source", saved], stdin),
                    {**summary(source, len(expected_report["losses"])), "sourceMatches": True, "report": expected_report})
            success(run(["verify", p, "--source", saved], stdin),
                    {**summary(source, len(expected_report["losses"])), "sourceMatches": True})
            for flags in (["--report", "--source", saved, "--strict-loss"], ["--strict-loss", "--source", saved, "--report"],
                          ["--strict-loss", "--report"]):
                result = run(["verify", p, *flags], stdin)
                if source == LOSSY:
                    failure(result, 2, {"error": "strict-loss rejected 2 loss(es)", "report": LOSSY_REPORT})
                else:
                    success(result, {**summary(source), **({"sourceMatches": True} if "--source" in flags else {}),
                                     "report": expected_report})
        assert snapshot(directory) == before


def test_source_mismatch_and_source_io_failure_precede_strict_policy_and_success_report(tmp_path):
    path, saved, _, _ = setup(tmp_path)
    saved.write_bytes(LOSSY + b" ")
    before = snapshot(tmp_path)
    for policy in ([], ["--strict-loss"]):
        failure(run(["verify", path, "--report", "--source", saved, *policy]), 1,
                {"error": "source does not match embedded original bytes"})
        result = run(["verify", path, "--report", "--source", tmp_path / "missing", *policy])
        assert result.status == 1 and result.stdout == ""
        error = json.loads(result.stderr)
        assert list(error) == ["error"] and "ENOENT" in error["error"]
    assert snapshot(tmp_path) == before


def _delete_normalized(b):
    del b["normalized"]


def _source_hash(b):
    b["sha256"]["source"] = "0" * 64


def _rehash_normalized(b):
    span(b["normalized"])["name"] = "tampered"
    b["sha256"]["normalized"] = sha(canonical_json(b["normalized"]))


def _rehash_report(b):
    b["lossReport"]["losses"].pop()
    b["sha256"]["lossReport"] = sha(canonical_json(b["lossReport"]))


def _recorded_strict(b):
    b["options"]["strictLoss"] = True
    b["sha256"]["options"] = sha(canonical_json(b["options"]))


TAMPERED = [
    ("schema", _delete_normalized, "bundle: missing normalized"),
    ("hash", _source_hash, "bundle.source: SHA-256 mismatch"),
    ("rehashed normalized", _rehash_normalized, "bundle.normalized: does not match offline conversion replay"),
    ("rehashed report", _rehash_report, "bundle.lossReport: does not match offline conversion replay"),
    ("recorded strict-loss", _recorded_strict, "bundle: offline conversion replay failed: strict-loss rejected 2 loss(es)"),
]


@pytest.mark.parametrize("name,mutate,message", TAMPERED, ids=[c[0] for c in TAMPERED])
def test_report_cannot_bypass_verification_before_source_or_policy(tmp_path, name, mutate, message):
    path, saved, bundle, _ = setup(tmp_path)
    mutate(bundle)
    text = wire(bundle)
    path.write_text(text, encoding="utf-8")
    before = snapshot(tmp_path)
    for p in (path, "-"):
        for extra in ([], ["--source", saved], ["--source", tmp_path / "missing"]):
            failure(run(["verify", p, "--report", "--strict-loss", *extra], text if p == "-" else None), 1,
                    {"error": message})
    assert snapshot(tmp_path) == before


def test_report_is_verify_only_and_duplicates_reject_before_reading_inputs():
    failure(run(["verify", "missing.json", "--report", "--report"]), 1, {"error": "duplicate option --report"})
    failure(run(["verify", "missing.json", "--report", "true"]), 1, {"error": "unknown option true for verify"})
    for flags in (["--source"], ["--source", "-"], ["--source", "a", "--source", "b"], ["--strict-loss", "--strict-loss"]):
        result = run(["verify", "missing.json", "--report", *flags])
        assert result.status == 1 and result.stdout == ""
        assert re.search("missing value|local regular file path|duplicate option", json.loads(result.stderr)["error"])
    for command in ("convert", "bundle", "extract"):
        failure(run([command, "missing.json", "--report"]), 1, {"error": f"unknown option --report for {command}"})
    for flag in ("--output", "--dry-run", "--from", "--zipkin-error-tag"):
        failure(run(["verify", "missing.json", "--report", flag]), 1, {"error": f"unknown option {flag} for verify"})
    help_result = run(["--help"])
    assert help_result.status == 0 and help_result.stderr == ""
    assert re.search(r"tracepack verify BUNDLE\|- \[--source SOURCE\] \[--strict-loss\] \[--report\]", help_result.stdout)
