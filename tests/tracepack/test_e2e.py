# SPDX-License-Identifier: Apache-2.0
"""CLI checks that use the stdlib JSON parser and hand-written goldens, not the converter."""
import json
import os
import re

import pytest

from .support import EXAMPLES, FIXTURES, example, jsdumps, run

ZIPKIN_EXAMPLE = EXAMPLES / "zipkin.json"


@pytest.mark.parametrize("source", ["zipkin", "jaeger", "otlp"])
@pytest.mark.parametrize("target", ["otlp", "jaeger", "zipkin"])
def test_e2e_file_to_atomic_output_matches_golden(tmp_path, source, target):
    output = tmp_path / "out.json"
    result = run(["convert", FIXTURES / f"common.{source}.json", "--from", source, "--to", target, "--strict-loss",
                  "--output", output])
    assert result.status == 0, result.stderr
    assert result.stdout == ""
    assert json.loads(output.read_text(encoding="utf-8")) == json.loads((FIXTURES / f"common.{target}.json").read_text())
    assert json.loads(result.stderr) == {"from": source, "to": target, "spanCount": 2, "losses": []}
    assert os.listdir(tmp_path) == ["out.json"]


def test_example_exact_nanoseconds_ids_and_service(tmp_path):
    output = tmp_path / "out.json"
    result = run(["convert", ZIPKIN_EXAMPLE, "--from", "zipkin", "--to", "otlp", "--output", output])
    assert result.status == 0, result.stderr
    trace = json.loads(output.read_text(encoding="utf-8"))
    r = trace["resourceSpans"][0]
    s = r["scopeSpans"][0]["spans"][0]
    assert r["resource"]["attributes"][0]["value"]["stringValue"] == "web"
    assert s["traceId"] == "1" * 32 and s["spanId"] == "2" * 16
    assert s["startTimeUnixNano"] == "1700000000000000000" and s["endTimeUnixNano"] == "1700000000001200000"
    assert int(s["endTimeUnixNano"]) - int(s["startTimeUnixNano"]) == 1200000
    assert json.loads(result.stderr)["losses"] == []


def test_otlp_to_zipkin_wire_output_and_normalized_return_match_goldens(tmp_path):
    output, back = tmp_path / "zipkin.json", tmp_path / "otlp.json"
    result = run(["convert", FIXTURES / "common.otlp.json", "--to", "zipkin", "--strict-loss", "--output", output])
    assert result.status == 0, result.stderr
    assert json.loads(output.read_text()) == json.loads((FIXTURES / "common.zipkin.json").read_text())
    roundtrip = run(["convert", output, "--to", "otlp", "--strict-loss", "--output", back])
    assert roundtrip.status == 0, roundtrip.stderr
    assert json.loads(back.read_text()) == json.loads((FIXTURES / "common.otlp.json").read_text())


def test_lossy_zipkin_projection_paths_and_strict_loss_or_dry_run_never_write(tmp_path):
    output, source = tmp_path / "zipkin.json", FIXTURES / "zipkin-lossy.otlp.json"
    normal = run(["convert", source, "--to", "zipkin"])
    assert normal.status == 0, normal.stderr
    assert json.loads(normal.stdout) == json.loads((FIXTURES / "zipkin-lossy.zipkin.json").read_text())
    report, p = json.loads(normal.stderr), "$.resourceSpans[0].scopeSpans[0].spans[0]"
    assert [loss["path"] for loss in report["losses"]] == [
        "$.resourceSpans[0].resource.attributes[1]", f"{p}.attributes[1]", f"{p}.attributes[2]", f"{p}.attributes[3]",
        f"{p}.endTimeUnixNano", f"{p}.events[0].attributes[0]", f"{p}.events[0].timeUnixNano",
        f"{p}.startTimeUnixNano", f'{p}["links"]']
    refused = run(["convert", source, "--to", "zipkin", "--strict-loss", "--output", output])
    assert refused.status == 2 and refused.stdout == ""
    assert json.loads(refused.stderr)["report"] == report and os.listdir(tmp_path) == []
    output.write_text("preserve existing bytes")
    for extra in ([], ["--dry-run"]):
        result = run(["convert", source, "--to", "zipkin", "--strict-loss", "--output", output, *extra])
        assert result.status == 2 and result.stdout == ""
        assert json.loads(result.stderr)["report"] == report
        assert output.read_text() == "preserve existing bytes" and os.listdir(tmp_path) == ["zipkin.json"]
    dry = run(["convert", source, "--to", "zipkin", "--dry-run", "--output", output])
    assert dry.status == 0, dry.stderr
    assert dry.stdout == "" and json.loads(dry.stderr) == report
    assert output.read_text() == "preserve existing bytes"
    stdout = run(["convert", source, "--to", "zipkin", "--strict-loss"])
    assert stdout.status == 2 and stdout.stdout == ""


def test_zipkin_minimum_duration_boundary_error_emits_no_bytes_and_preserves_destination(tmp_path):
    output = tmp_path / "zipkin.json"
    doc = json.loads((FIXTURES / "common.otlp.json").read_text())
    doc["resourceSpans"] = doc["resourceSpans"][:1]
    s = doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    s["startTimeUnixNano"] = s["endTimeUnixNano"] = "18446744073709551615"
    output.write_text("sentinel")
    result = run(["convert", "-", "--to", "zipkin", "--output", output], jsdumps(doc))
    assert result.status == 1 and result.stdout == ""
    assert "minimum duration would exceed uint64" in json.loads(result.stderr)["error"]
    assert output.read_text() == "sentinel" and os.listdir(tmp_path) == ["zipkin.json"]


def test_stdin_stdout_conversion_and_report_channels_are_separate():
    result = run(["convert", "-", "--to", "jaeger"], example("zipkin.json"))
    assert result.status == 0, result.stderr
    s = json.loads(result.stdout)["data"][0]["spans"][0]
    assert s["startTime"] == 1700000000000000 and s["duration"] == 1200
    assert type(s["startTime"]) is int
    assert json.loads(result.stderr)["spanCount"] == 1


def test_microsecond_json_numbers_above_safe_integer_remain_exact_end_to_end():
    text = example("zipkin.json").replace("1700000000000000", "9007199254740993", 1)
    result = run(["convert", "-", "--to", "jaeger", "--strict-loss"], text)
    assert result.status == 0, result.stderr
    assert re.search(r'"startTime": 9007199254740993,', result.stdout)
    otlp = run(["convert", "-", "--to", "otlp", "--strict-loss"], result.stdout)
    assert otlp.status == 0, otlp.stderr
    assert json.loads(otlp.stdout)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["startTimeUnixNano"] == \
        "9007199254740993000"


def test_dry_run_validates_but_creates_no_output_and_never_executes_input(tmp_path):
    output, path = tmp_path / "out.json", tmp_path / "trace.json"
    source = json.loads(example("zipkin.json"))
    source[0]["name"] = f"$(touch {tmp_path / 'executed'})"
    path.write_text(jsdumps(source), encoding="utf-8")
    result = run(["convert", path, "--to", "otlp", "--dry-run", "--output", output])
    assert result.status == 0, result.stderr
    assert result.stdout == ""
    assert os.listdir(tmp_path) == ["trace.json"] and json.loads(result.stderr)["losses"] == []


def test_strict_loss_fails_with_structured_manifest_and_unchanged_existing_output(tmp_path):
    path, output = tmp_path / "input.json", tmp_path / "out.json"
    source = json.loads(example("zipkin.json"))
    source[0]["debug"] = True
    path.write_text(jsdumps(source), encoding="utf-8")
    output.write_text("preserve existing bytes")
    result = run(["convert", path, "--to", "otlp", "--strict-loss", "--output", output])
    assert result.status == 2 and result.stdout == ""
    assert output.read_text() == "preserve existing bytes"
    assert json.loads(result.stderr)["report"]["losses"][0]["path"] == '$[0]["debug"]'
    assert sorted(os.listdir(tmp_path)) == ["input.json", "out.json"]
    dry = run(["convert", path, "--to", "otlp", "--strict-loss", "--dry-run"])
    assert dry.status == 2 and dry.stdout == ""


def _conflict():
    source = json.loads(example("zipkin.json"))
    source.append({**source[0], "name": "conflict"})
    return jsdumps(source).encode()


@pytest.mark.parametrize("name,content,expected", [
    ("invalid JSON", b"{", "Expected object key"),
    ("invalid ID", b'[{"traceId":"0","id":"0","name":"x","timestamp":1,"duration":1}]', "nonzero"),
    ("conflicting duplicate", None, "conflicting duplicate"),
    ("invalid UTF8", bytes([0xC3, 0x28]), "encoded data"),
])
def test_cli_failure_creates_no_partial_output(tmp_path, name, content, expected):
    path, output = tmp_path / "input.json", tmp_path / "out.json"
    path.write_bytes(_conflict() if content is None else content)
    result = run(["convert", path, "--to", "otlp", "--output", output])
    assert result.status == 1 and result.stdout == ""
    assert re.search(expected, json.loads(result.stderr)["error"])
    assert os.listdir(tmp_path) == ["input.json"]


def test_non_file_destination_rejected_before_temporary_file_creation(tmp_path):
    output = tmp_path / "directory"
    output.mkdir()
    result = run(["convert", ZIPKIN_EXAMPLE, "--to", "otlp", "--output", output])
    assert result.status == 1 and "regular file" in result.stderr
    assert os.listdir(tmp_path) == ["directory"]


def test_symlink_output_is_refused_without_modifying_its_target(tmp_path):
    target, output = tmp_path / "target", tmp_path / "link"
    target.write_text("sentinel")
    os.symlink(target, output)
    result = run(["convert", ZIPKIN_EXAMPLE, "--to", "otlp", "--output", output])
    assert result.status == 1 and target.read_text() == "sentinel"


def test_successful_atomic_overwrite_replaces_complete_file_and_leaves_no_temporary_files(tmp_path):
    output = tmp_path / "out.json"
    output.write_text("old")
    result = run(["convert", ZIPKIN_EXAMPLE, "--to", "otlp", "--output", output])
    assert result.status == 0, result.stderr
    assert json.loads(output.read_text())["resourceSpans"]
    assert os.listdir(tmp_path) == ["out.json"]


@pytest.mark.parametrize("args", [[], ["convert"], ["convert", str(ZIPKIN_EXAMPLE)], ["convert", "-", "--to", "unknown"],
                                  ["convert", "-", "--unknown"], ["convert", "-", "--to"],
                                  ["convert", "-", "--to", "otlp", "--to", "otlp"]], ids=jsdumps)
def test_cli_invalid_arguments(args):
    result = run(args, "[]")
    assert result.status == 1 and result.stdout == "" and result.stderr


def test_cli_missing_input_and_missing_output_parent_fail_explicitly(tmp_path):
    a = run(["convert", tmp_path / "absent", "--to", "otlp"])
    assert a.status == 1 and "ENOENT" in a.stderr
    b = run(["convert", ZIPKIN_EXAMPLE, "--to", "otlp", "--output", tmp_path / "absent" / "out.json"])
    assert b.status == 1 and "ENOENT" in b.stderr
    assert os.listdir(tmp_path) == []


def test_cli_help_documents_supported_interface():
    result = run(["--help"])
    assert result.status == 0 and "--strict-loss" in result.stdout and result.stderr == ""


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root bypasses permission checks")
def test_unwritable_destination_preserves_existing_bytes_and_emits_one_json_error(tmp_path):
    directory = tmp_path / "locked"
    directory.mkdir()
    output = directory / "out.json"
    output.write_text("sentinel")
    os.chmod(directory, 0o500)
    try:
        result = run(["convert", ZIPKIN_EXAMPLE, "--to", "otlp", "--output", output])
        assert result.status == 1 and result.stdout == ""
        error = json.loads(result.stderr)
        assert "EACCES" in error["error"] and error["report"]["losses"] == []
        assert output.read_text() == "sentinel" and os.listdir(directory) == ["out.json"]
    finally:
        os.chmod(directory, 0o700)


def test_64_mib_input_cap_fails_without_writing_output(tmp_path):
    path, output = tmp_path / "large.json", tmp_path / "out.json"
    path.write_bytes(b" " * (64 * 1024 * 1024 + 1))
    result = run(["convert", path, "--to", "otlp", "--output", output])
    assert result.status == 1 and "64 MiB" in json.loads(result.stderr)["error"]
    assert os.listdir(tmp_path) == ["large.json"]
