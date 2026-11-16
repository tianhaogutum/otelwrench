# SPDX-License-Identifier: Apache-2.0
import json
import os
import re
import stat
import sys

import pytest

from otelwrench.tracepack import canonical_json, create_bundle, stringify

from .support import ORIGINAL, SOURCE_LIMIT, b64, fixture, run, sha, snapshot, span, sparse, unb64, wire


def summary(data):
    return {"valid": True, "format": "tracepack-fixture", "version": 1, "sourceSha256": sha(data), "spanCount": 1,
            "lossCount": 0, "guarantee": "reproducibility-only"}


def failure(result, expected):
    assert result.status == 1, result.stderr
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert list(error) == ["error"]
    assert re.search(expected, error["error"])


CHANGES = [
    ("BOM removal", lambda s: s[1:]),
    ("CRLF to LF", lambda s: s.replace("\r\n", "\n")),
    ("whitespace only", lambda s: s + " \t"),
    ("unsafe exponent spelling", lambda s: s.replace("9007199254740993", "9.007199254740993e15", 1)),
    ("unsafe decimal spelling", lambda s: s.replace("9007199254740993", "9007199254740993.0", 1)),
]


def test_verify_source_accepts_exact_bytes_from_file_and_stdin_bundles_without_writes(tmp_path):
    source, bundle = tmp_path / "source.json", tmp_path / "bundle.json"
    source.write_bytes(ORIGINAL)
    bundle.write_text(wire(create_bundle(ORIGINAL)), encoding="utf-8")
    before = snapshot(tmp_path)
    for path in (bundle, "-"):
        result = run(["verify", path, "--source", source], bundle.read_bytes() if path == "-" else None)
        assert result.status == 0, result.stderr
        assert result.stderr == ""
        assert result.stdout == stringify({**summary(ORIGINAL), "sourceMatches": True})
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("name,change", CHANGES, ids=[c[0] for c in CHANGES])
def test_verify_source_rejects_changed_bytes_despite_identical_normalization(tmp_path, name, change):
    source = tmp_path / "source.json"
    different = change(ORIGINAL.decode("utf-8")).encode("utf-8")
    assert different != ORIGINAL
    bundle, other = create_bundle(ORIGINAL), create_bundle(different)
    assert canonical_json(bundle["normalized"]) == canonical_json(other["normalized"])
    assert bundle["lossReport"] == other["lossReport"]
    source.write_bytes(different)
    before = snapshot(tmp_path)
    failure(run(["verify", "-", "--source", source], wire(bundle)), r"^source does not match embedded original bytes$")
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("fmt", ["otlp", "jaeger", "zipkin"])
def test_verify_source_accepts_exact_bytes_and_rejects_empty_files(tmp_path, fmt):
    source, data = tmp_path / "source.json", fixture(f"common.{fmt}.json").encode()
    text = wire(create_bundle(data))
    source.write_bytes(data)
    result = run(["verify", "-", "--source", source], text)
    assert result.status == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == {**summary(data), "spanCount": 2, "sourceMatches": True}
    source.write_bytes(b"")
    failure(run(["verify", "-", "--source", source], text), "source does not match")


def test_verify_source_compares_unsupported_original_fields(tmp_path):
    source = tmp_path / "source.json"
    data = ORIGINAL.decode().replace('"duration":1', '"duration":1,"debug":true').encode()
    bundle = create_bundle(data)
    assert len(bundle["lossReport"]["losses"]) == 1
    source.write_bytes(data)
    result = run(["verify", "-", "--source", source], wire(bundle))
    assert result.status == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == {**summary(data), "lossCount": 1, "sourceMatches": True}
    source.write_bytes(ORIGINAL)
    failure(run(["verify", "-", "--source", source], wire(bundle)), "source does not match")


@pytest.mark.parametrize("member", ["source", "normalized", "lossReport"])
def test_matching_source_cannot_bypass_rehashed_inconsistent_member(tmp_path, member):
    source, missing = tmp_path / "source.json", tmp_path / "missing"
    bundle = create_bundle(ORIGINAL)
    if member == "source":
        bundle["source"]["data"] = b64(b"[]")
    if member == "normalized":
        span(bundle["normalized"])["name"] = "tampered"
    if member == "lossReport":
        bundle["lossReport"]["spanCount"] = 999
    embedded = unb64(bundle["source"]["data"])
    bundle["sha256"][member] = sha(embedded if member == "source" else canonical_json(bundle[member]))
    source.write_bytes(embedded)
    before = snapshot(tmp_path)
    for path in (source, missing):
        failure(run(["verify", "-", "--source", path], wire(bundle)), "replay")
    assert snapshot(tmp_path) == before


def test_verify_source_refuses_missing_directory_symlink_and_device_paths(tmp_path):
    source, link, directory = tmp_path / "source", tmp_path / "link", tmp_path / "directory"
    source.write_bytes(ORIGINAL)
    os.symlink(source, link)
    directory.mkdir()
    before, text = snapshot(tmp_path), wire(create_bundle(ORIGINAL))
    failure(run(["verify", "-", "--source", tmp_path / "missing"], text), "ENOENT")
    for path in (directory, link, *(["/dev/null"] if sys.platform.startswith("linux") else [])):
        failure(run(["verify", "-", "--source", path], text), "source must be a regular file")
    assert snapshot(tmp_path) == before


@pytest.mark.skipif(sys.platform == "win32", reason="FIFOs require POSIX")
def test_verify_source_refuses_a_fifo_without_blocking(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    failure(run(["verify", "-", "--source", fifo], wire(create_bundle(ORIGINAL))), "source must be a regular file")
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode) and os.listdir(tmp_path) == ["fifo"]


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root bypasses permission checks")
def test_verify_source_reports_unreadable_regular_files_with_no_success_bytes(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(ORIGINAL)
    os.chmod(source, 0)
    try:
        failure(run(["verify", "-", "--source", source], wire(create_bundle(ORIGINAL))), "EACCES")
        assert os.lstat(source).st_mode & 0o777 == 0
    finally:
        os.chmod(source, 0o600)


def test_verify_source_enforces_the_64_mib_source_limit(tmp_path):
    source = tmp_path / "source"
    sparse(source, SOURCE_LIMIT + 1)
    failure(run(["verify", "-", "--source", source], wire(create_bundle(ORIGINAL))), "64 MiB limit")
    assert os.lstat(source).st_size == SOURCE_LIMIT + 1 and os.listdir(tmp_path) == ["source"]


def test_verify_source_requires_one_explicit_path_and_is_restricted_to_verify(tmp_path):
    missing = tmp_path / "missing"
    for extra, expected in ((["--source"], "missing value"), (["--source", "--help"], "missing value"),
                            (["--source", "-"], "source requires.*file path"),
                            (["--source", missing, "--source", missing], "duplicate option")):
        failure(run(["verify", missing, *extra]), expected)
    for command in ("convert", "bundle", "extract"):
        failure(run([command, missing, "--source", missing]), "unknown option --source")
    (tmp_path / "-").write_bytes(ORIGINAL)
    literal = run(["verify", "-", "--source", "./-"], wire(create_bundle(ORIGINAL)), cwd=tmp_path)
    assert literal.status == 0, literal.stderr
    assert json.loads(literal.stdout)["sourceMatches"] is True
    assert os.listdir(tmp_path) == ["-"]
    help_result = run(["--help"])
    assert help_result.status == 0 and help_result.stderr == ""
    assert re.search(r"tracepack verify BUNDLE\|- \[--source SOURCE\]", help_result.stdout)


def test_plain_verify_preserves_the_exact_existing_summary_without_source_matches():
    result = run(["verify", "-"], wire(create_bundle(ORIGINAL)))
    assert result.status == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == stringify(summary(ORIGINAL))
