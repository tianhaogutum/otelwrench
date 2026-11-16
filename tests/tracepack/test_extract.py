# SPDX-License-Identifier: Apache-2.0
import json
import os
import re

import pytest

from otelwrench.tracepack import canonical_json, create_bundle, extract_bundle, verify_bundle

from .support import b64, fixture, ino, mode, run, sha, span, wire

ORIGINAL = ("﻿\r\n\t" + fixture("common.zipkin.json").replace("1700000000000000", "9007199254740993", 1)
            .replace("GET /", "GET /café/雪", 1) + "\r\n ").encode()


def success(result):
    assert result.status == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def failure(result, expected):
    assert result.status == 1, result.stderr
    assert result.stdout == ""
    assert re.search(expected, json.loads(result.stderr)["error"])


def test_extract_bundle_returns_exact_bytes_including_bom_whitespace_and_unsafe_integers():
    text = wire(create_bundle(ORIGINAL))
    result = extract_bundle(text)
    assert isinstance(result, bytes) and result == ORIGINAL
    assert sha(result) == verify_bundle(text)["sourceSha256"]
    assert verify_bundle(text)["guarantee"] == "reproducibility-only"


@pytest.mark.parametrize("source", ["zipkin", "jaeger", "otlp"])
def test_cli_extracts_exact_source_to_a_new_file(tmp_path, source):
    bundle, output = tmp_path / "fixture.json", tmp_path / "source.json"
    data = ORIGINAL if source == "zipkin" else fixture(f"common.{source}.json").encode()
    bundle.write_text(wire(create_bundle(data)), encoding="utf-8")
    success(run(["extract", bundle, "--output", output]))
    assert output.read_bytes() == data and mode(output) == 0o600
    assert sorted(os.listdir(tmp_path)) == ["fixture.json", "source.json"]


def test_stdin_extraction_replaces_the_destination_atomically(tmp_path):
    output, old_link = tmp_path / "source", tmp_path / "old-inode"
    output.write_text("PRESERVE\n")
    os.chmod(output, 0o644)
    os.link(output, old_link)
    old_inode = ino(output)
    success(run(["extract", "-", "--output", output], wire(create_bundle(ORIGINAL))))
    assert output.read_bytes() == ORIGINAL and mode(output) == 0o600
    assert ino(output) != old_inode
    assert old_link.read_text() == "PRESERVE\n"
    assert sorted(os.listdir(tmp_path)) == ["old-inode", "source"]


def test_lossy_normalization_still_extracts_unsupported_fields_and_literal_shell_text(tmp_path):
    output, marker = tmp_path / "source", tmp_path / "executed"
    data = ('[ {"traceId":"11111111111111111111111111111111","id":"2222222222222222","name":"$(touch '
            f'{marker})","timestamp":1,"duration":1,"debug":true}} ]\r\n').encode()
    bundle = create_bundle(data)
    assert len(bundle["lossReport"]["losses"]) > 0
    success(run(["extract", "-", "--output", output], wire(bundle)))
    assert output.read_bytes() == data and os.listdir(tmp_path) == ["source"]


def _tamper_source(b):
    b["source"]["data"] = b64(b"[]")


def _rehash_normalized(b):
    span(b["normalized"])["name"] = "tampered"
    b["sha256"]["normalized"] = sha(canonical_json(b["normalized"]))


def _rehash_report(b):
    b["lossReport"]["spanCount"] = 999
    b["sha256"]["lossReport"] = sha(canonical_json(b["lossReport"]))


def _missing_report(b):
    del b["lossReport"]


def _version(b):
    b["converter"]["version"] = "999.0.0"


def _base64(b):
    b["source"]["data"] += "!"


BAD_BUNDLES = [
    ("source hash", _tamper_source, "source.*SHA-256"),
    ("rehashed normalization", _rehash_normalized, "normalized.*replay"),
    ("rehashed report", _rehash_report, "lossReport.*replay"),
    ("missing report", _missing_report, "missing lossReport"),
    ("version mismatch", _version, "converter.*version"),
    ("invalid base64", _base64, "base64"),
    ("malformed JSON", lambda b: "{", "Expected object key"),
    ("invalid UTF-8", lambda b: bytes([0xFF]), "encoded data"),
]


@pytest.mark.parametrize("name,mutate,expected", BAD_BUNDLES, ids=[c[0] for c in BAD_BUNDLES])
def test_extract_refuses_before_touching_any_destination(tmp_path, name, mutate, expected):
    output, absent = tmp_path / "source", tmp_path / "absent"
    bundle = create_bundle(ORIGINAL)
    replacement = mutate(bundle)
    text = replacement if replacement is not None else wire(bundle)
    if isinstance(text, str):
        with pytest.raises(Exception, match=expected):
            extract_bundle(text)
    output.write_text("PRESERVE\n")
    os.chmod(output, 0o644)
    before = os.lstat(output)
    for target in (output, absent):
        failure(run(["extract", "-", "--output", target], text), expected)
    after = os.lstat(output)
    assert output.read_text() == "PRESERVE\n"
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (before.st_ino, before.st_mode, before.st_mtime_ns)
    assert os.listdir(tmp_path) == ["source"]


def test_extract_reads_and_verifies_before_same_path_replacement(tmp_path):
    path = tmp_path / "fixture.json"
    invalid = create_bundle(ORIGINAL)
    invalid["sha256"]["source"] = "0" * 64
    invalid_text = wire(invalid)
    path.write_text(invalid_text, encoding="utf-8")
    failure(run(["extract", path, "--output", path]), "SHA-256")
    assert path.read_text(encoding="utf-8") == invalid_text
    path.write_text(wire(create_bundle(ORIGINAL)), encoding="utf-8")
    success(run(["extract", path, "--output", path]))
    assert path.read_bytes() == ORIGINAL
    assert os.listdir(tmp_path) == ["fixture.json"]


def test_extract_requires_a_file_output_and_rejects_conversion_flags_before_io(tmp_path):
    path, missing = tmp_path / "source", tmp_path / "missing-bundle"
    cases = [([], "(?i)--output.*file"), (["--output", "-"], "(?i)--output.*file"), (["--output"], "missing value"),
             (["--output", path, "--output", path], "duplicate option")]
    cases += [(["--output", path, flag], "unknown option") for flag in ("--from", "--to", "--dry-run", "--zipkin-error-tag")]
    for extra, expected in cases:
        failure(run(["extract", missing, *extra]), expected)
    assert os.listdir(tmp_path) == []
    help_result = run(["--help"])
    assert help_result.status == 0
    assert re.search(r"tracepack extract BUNDLE\|- --output FILE", help_result.stdout)


def test_extract_refuses_symlinks_directories_and_missing_parents(tmp_path):
    target, link, directory = tmp_path / "target", tmp_path / "link", tmp_path / "directory"
    target.write_text("PRESERVE")
    os.symlink(target, link)
    directory.mkdir()
    text = wire(create_bundle(ORIGINAL))
    for path in (link, directory):
        failure(run(["extract", "-", "--output", path], text), "regular file")
    failure(run(["extract", "-", "--output", tmp_path / "missing" / "source"], text), "ENOENT")
    assert target.read_text() == "PRESERVE" and link.is_symlink()
    assert os.listdir(directory) == []
    assert sorted(os.listdir(tmp_path)) == ["directory", "link", "target"]
