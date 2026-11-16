# SPDX-License-Identifier: Apache-2.0
import json
import os
import sys

import pytest

from otelwrench.tracepack import convert

from .support import SOURCE_LIMIT, fixture, ino, json2, mode, no_access_hooks, run, run_process, snapshot, sparse

MAPPING_ERROR = "--zipkin-error-tag: only supported for OTLP or Jaeger to Zipkin"
PAIRS = [["--to", "otlp"], ["--to", "jaeger"], ["--from", "zipkin", "--to", "zipkin"]]
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="FIFOs require POSIX")


def refused(result, error=MAPPING_ERROR):
    assert result.status == 1, result.stderr
    assert result.stdout == ""
    assert result.stderr == json2({"error": error})


def setup(tmp_path):
    source, output = tmp_path / "source", tmp_path / "output"
    source.write_bytes(b"[ ]\r\n")
    output.write_bytes(b"PRESERVE\r\n")
    os.chmod(output, 0o640)
    return source, output


def snap(directory):
    return snapshot(directory, small_only=True, with_size=True)


@posix_only
@pytest.mark.parametrize("flags", PAIRS, ids=" ".join)
def test_mapping_rejects_unread_explicit_stdin_promptly(tmp_path, flags):
    _, output = setup(tmp_path)
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    # A real writer remains open without bytes or EOF.
    keeper = os.open(pipe, os.O_RDWR)
    reader = os.open(pipe, os.O_RDONLY)
    before = snap(tmp_path)
    try:
        refused(run_process(["convert", "-", *flags, "--zipkin-error-tag", "--strict-loss", "--dry-run",
                             "--output", output], stdin=reader))
        assert snap(tmp_path) == before
    finally:
        os.close(reader)
        os.close(keeper)


def test_known_incompatible_pairs_precede_primary_and_destination_access(tmp_path):
    source, output = setup(tmp_path)
    absent, link, large, directory = (tmp_path / n for n in ("absent", "link", "large", "directory"))
    os.symlink(source, link)
    directory.mkdir()
    sparse(large, SOURCE_LIMIT + 1)
    inputs = [source, link, absent, directory, large, *(["/dev/zero"] if sys.platform.startswith("linux") else []), "-"]
    before = snap(tmp_path)
    for flags in PAIRS:
        for path in inputs:
            for destination in (output, absent, path):
                refused(run(["convert", path, *flags, "--zipkin-error-tag", "--strict-loss", "--dry-run",
                             "--output", destination], hooks=no_access_hooks()))
                assert snap(tmp_path) == before
    # Exercise actual filesystem failures too, without a hook or dry-run.
    for flags in PAIRS:
        for path in (absent, directory, large, source):
            refused(run(["convert", path, *flags, "--zipkin-error-tag", "--strict-loss", "--output", path]))
            assert snap(tmp_path) == before


def test_syntax_then_format_faults_precede_mapping_compatibility_and_help_short_circuits():
    prefix = ["convert", "-", "--to", "otlp", "--zipkin-error-tag"]
    for tail, error in (
        (["--zipkin-error-tag"], "duplicate option --zipkin-error-tag"),
        (["--to", "zipkin"], "duplicate option --to"),
        (["--output"], "missing value for --output"),
        (["--from"], "missing value for --from"),
        (["--report"], "unknown option --report for convert"),
        (["--unknown", "--output"], "unknown option --unknown for convert"),
        (["--from", "bad"], "--from: expected auto, otlp, jaeger or zipkin"),
    ):
        refused(run([*prefix, *tail], hooks=no_access_hooks()), error)
    for flags in ([], ["--from", "zipkin"], ["--from", "bad"]):
        expected = ("--from: expected auto, otlp, jaeger or zipkin" if flags[1:] == ["bad"]
                    else "--to: expected otlp, jaeger or zipkin")
        refused(run(["convert", "-", "--zipkin-error-tag", *flags, "--to", "bad"], hooks=no_access_hooks()), expected)
    refused(run(["convert", "-", "--zipkin-error-tag", "--from", "zipkin"], hooks=no_access_hooks()),
            "--to: expected otlp, jaeger or zipkin")
    for flags in (PAIRS[0], PAIRS[2]):
        result = run(["convert", "-", *flags, "--zipkin-error-tag", "--help"], hooks=no_access_hooks())
        assert result.status == 0, result.stderr
        assert result.stdout.startswith("otelwrench tracepack")
        assert result.stderr == ""


def test_every_supported_but_incompatible_pair_rejects_even_malformed_source_bytes():
    for source in ("auto", "otlp", "jaeger", "zipkin"):
        for target in ("otlp", "jaeger", "zipkin"):
            if target == "zipkin" and source != "zipkin":
                continue
            refused(run(["convert", "-", "--from", source, "--to", target, "--zipkin-error-tag"], "{"))


def test_compatible_sources_preserve_golden_output_report_and_atomic_in_place_replacement(tmp_path):
    source, _ = setup(tmp_path)
    for fmt in ("otlp", "jaeger"):
        for selection in ([], ["--from", "auto"], ["--from", fmt]):
            text, expected = fixture(f"common.{fmt}.json"), fixture("common.zipkin.json")
            flags = [*selection, "--to", "zipkin", "--zipkin-error-tag", "--strict-loss"]
            stdin = run(["convert", "-", *flags], text)
            assert stdin.status == 0, stdin.stderr
            assert json.loads(stdin.stdout) == json.loads(expected)
            assert json.loads(stdin.stderr) == {"from": fmt, "to": "zipkin", "spanCount": 2, "losses": []}
            source.write_text(text, encoding="utf-8")
            os.chmod(source, 0o640)
            old = tmp_path / "old"
            os.link(source, old)
            before = snap(tmp_path)
            dry = run(["convert", source, *flags, "--output", source, "--dry-run"])
            assert dry.status == 0, dry.stderr
            assert dry.stdout == "" and dry.stderr == stdin.stderr
            assert snap(tmp_path) == before
            written = run(["convert", source, *flags, "--output", source])
            assert written.status == 0, written.stderr
            assert written.stdout == "" and written.stderr == stdin.stderr
            assert source.read_text(encoding="utf-8") == stdin.stdout
            assert old.read_text(encoding="utf-8") == text
            assert ino(source) != ino(old) and mode(source) == 0o600
            old.unlink()
            assert sorted(os.listdir(tmp_path)) == ["output", "source"]


def test_source_dependent_checks_and_library_precedence_stay_post_parse():
    flags = ["convert", "-", "--to", "zipkin", "--zipkin-error-tag"]
    refused(run(flags, "[]"))
    malformed = run(flags, "{")
    assert malformed.status == 1 and malformed.stdout == ""
    assert "Expected object key" in json.loads(malformed.stderr)["error"]
    for pair in ({"to": "otlp"}, {"from": "zipkin", "to": "zipkin"}):
        with pytest.raises(Exception, match="Expected object key"):
            convert("{", {**pair, "zipkinErrorTag": True})
        with pytest.raises(Exception) as info:
            convert("[]", {**pair, "zipkinErrorTag": True})
        assert str(info.value) == MAPPING_ERROR
    with pytest.raises(Exception, match="options.zipkinErrorTag: expected boolean"):
        convert("{", {"to": "zipkin", "zipkinErrorTag": "true"})
