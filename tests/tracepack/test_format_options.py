# SPDX-License-Identifier: Apache-2.0
import json
import os
import re
import sys

import pytest

from otelwrench.tracepack import convert, create_bundle

from .support import SOURCE_LIMIT, b64, json2, no_access_hooks, run, run_process, snapshot, sparse

FROM_ERROR = "--from: expected auto, otlp, jaeger or zipkin"
TO_ERROR = "--to: expected otlp, jaeger or zipkin"
FAULTS = [
    ("convert", [], TO_ERROR),
    ("convert", ["--to", "invalid"], TO_ERROR),
    ("convert", ["--from", "invalid", "--to", "otlp"], FROM_ERROR),
    ("bundle", ["--from", "invalid"], FROM_ERROR),
]
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="FIFOs require POSIX")


def setup(tmp_path):
    source, output = tmp_path / "source", tmp_path / "output"
    source.write_bytes(b"[ ]\r\n")
    output.write_bytes(b"PRESERVE\r\n")
    os.chmod(output, 0o640)
    return source, output


def snap(directory):
    return snapshot(directory, small_only=True, with_size=True)


def refused(result, error):
    assert result.status == 1, result.stderr
    assert result.stdout == ""
    # Literal wire contract: exactly one JSON error, no conversion/policy report.
    assert result.stderr == json2({"error": error})


@posix_only
@pytest.mark.parametrize("command,flags,error", FAULTS)
def test_rejects_unread_explicit_stdin_promptly(tmp_path, command, flags, error):
    _, output = setup(tmp_path)
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    # Keep a real FIFO writer alive without sending bytes or EOF.
    keeper = os.open(pipe, os.O_RDWR)
    reader = os.open(pipe, os.O_RDONLY)
    before = snap(tmp_path)
    try:
        result = run_process([command, "-", *flags, "--strict-loss", "--output", output,
                              *(["--dry-run"] if command == "convert" else [])], stdin=reader)
        refused(result, error)
        assert snap(tmp_path) == before
    finally:
        os.close(reader)
        os.close(keeper)


def test_format_faults_precede_all_source_and_destination_access(tmp_path):
    source, output = setup(tmp_path)
    absent, link, large, directory = (tmp_path / n for n in ("absent", "link", "large", "directory"))
    os.symlink(source, link)
    directory.mkdir()
    sparse(large, SOURCE_LIMIT + 1)
    inputs = [source, link, absent, directory, large, *(["/dev/zero"] if sys.platform.startswith("linux") else []), "-"]
    before = snap(tmp_path)
    for command, flags, error in FAULTS:
        for path in inputs:
            for destination in (output, absent, path):
                extra = ["--dry-run"] if command == "convert" and destination == path else []
                refused(run([command, path, *flags, "--output", destination, "--strict-loss", *extra],
                            hooks=no_access_hooks()), error)
                assert snap(tmp_path) == before


def test_semantic_format_errors_have_fixed_from_before_to_precedence():
    for flags in (["--from", "bad"], ["--from", "bad", "--to", "bad"], ["--to", "bad", "--from", "bad"]):
        refused(run(["convert", "-", *flags], hooks=no_access_hooks()), FROM_ERROR)
    for value in ("AUTO", "OTLP", "-", "auto", "invalid"):
        refused(run(["convert", "-", "--to", value], hooks=no_access_hooks()), TO_ERROR)
    for value in ("AUTO", "OTLP", "-", "invalid"):
        for command in ("convert", "bundle"):
            refused(run([command, "-", "--from", value, *(["--to", "otlp"] if command == "convert" else [])],
                        hooks=no_access_hooks()), FROM_ERROR)


def test_duplicate_foreign_and_missing_value_syntax_keeps_left_to_right_priority():
    for command, prefix in (("convert", ["--to", "bad"]), ("bundle", ["--from", "bad"])):
        for tail, error in (
            (["--from", "auto", "--from", "otlp"], "duplicate option --from"),
            (["--output", "a", "--output", "b"], "duplicate option --output"),
            (["--strict-loss", "--strict-loss"], "duplicate option --strict-loss"),
            (["--output"], "missing value for --output"),
            (["--from"], "missing value for --from"),
            (["--source", "a"], f"unknown option --source for {command}"),
            (["--report"], f"unknown option --report for {command}"),
            (["--unknown", "--output"], f"unknown option --unknown for {command}"),
            (["--output", "--unknown"], "missing value for --output"),
        ):
            # Avoid an intentional earlier duplicate --from in the bundle prefix.
            start = [] if command == "bundle" and tail[0] == "--from" else prefix
            refused(run([command, "-", *start, *tail], hooks=no_access_hooks()), error)
    refused(run(["convert", "-", "--from", "bad", "--to"], hooks=no_access_hooks()), "missing value for --to")
    refused(run(["convert", "-", "--to", "bad", "--to", "otlp"], hooks=no_access_hooks()), "duplicate option --to")
    refused(run(["bundle", "-", "--from", "bad", "--to", "otlp"], hooks=no_access_hooks()), "unknown option --to for bundle")
    for command in ("verify", "extract"):
        for flag in ("--from", "--to"):
            refused(run([command, "-", flag, "bad"], hooks=no_access_hooks()), f"unknown option {flag} for {command}")
    for command in ("convert", "bundle"):
        result = run([command, "-", "--from", "bad", "--help"], hooks=no_access_hooks())
        assert result.status == 0, result.stderr
        assert re.match(r"^otelwrench tracepack", result.stdout)
        assert result.stderr == ""


def test_supported_from_values_and_targets_remain_accepted_with_auto_detection_and_stdin():
    inputs = {"otlp": '{"resourceSpans":[]}', "jaeger": '{"data":[]}', "zipkin": "[]"}
    for source, text in inputs.items():
        for selection in ([], ["--from", "auto"], ["--from", source]):
            for target in ("otlp", "jaeger", "zipkin"):
                result = run(["convert", "-", *selection, "--to", target, "--strict-loss"], text)
                assert result.status == 0, result.stderr
                assert json.loads(result.stderr) == {"from": source, "to": target, "spanCount": 0, "losses": []}
                assert json.loads(result.stdout) == json.loads(inputs[target])
            bundle = run(["bundle", "-", *selection, "--strict-loss"], text)
            assert bundle.status == 0, bundle.stderr
            assert json.loads(bundle.stderr) == {"from": source, "to": "otlp", "spanCount": 0, "losses": []}
            assert json.loads(bundle.stdout)["source"]["data"] == b64(text.encode())


def test_mapping_validation_still_follows_input_parse_detection_and_format_preflight():
    refused(run(["convert", "-", "--to", "bad", "--zipkin-error-tag"], hooks=no_access_hooks()), TO_ERROR)
    refused(run(["convert", "-", "--to", "zipkin", "--zipkin-error-tag"], "[]"),
            "--zipkin-error-tag: only supported for OTLP or Jaeger to Zipkin")
    malformed = run(["convert", "-", "--to", "zipkin", "--zipkin-error-tag"], "{")
    assert malformed.status == 1 and malformed.stdout == ""
    assert "Expected object key" in json.loads(malformed.stderr)["error"]


def test_library_format_contracts_and_validation_ordering_remain_unchanged():
    with pytest.raises(Exception) as info:
        convert("{", {"from": "bad", "to": "bad"})
    assert str(info.value) == FROM_ERROR
    with pytest.raises(Exception) as info:
        convert("{", {"to": "bad"})
    assert str(info.value) == TO_ERROR
    with pytest.raises(Exception) as info:
        convert("{")
    assert str(info.value) == TO_ERROR
    with pytest.raises(Exception, match="options.zipkinErrorTag: expected boolean"):
        convert("[]", {"to": "otlp", "zipkinErrorTag": "true"})
    with pytest.raises(Exception) as info:
        create_bundle("[]", {"from": "bad"})
    assert str(info.value) == FROM_ERROR
    with pytest.raises(Exception, match="encoded data"):
        create_bundle(bytes([0xFF]), {"from": "bad"})
    with pytest.raises(Exception, match="options.strictLoss: expected boolean"):
        create_bundle("[]", {"from": "bad", "strictLoss": "true"})
