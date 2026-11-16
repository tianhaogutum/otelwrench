# SPDX-License-Identifier: Apache-2.0
import os

import pytest

from otelwrench.tracepack import canonical_json, create_bundle, stringify

from .support import LOSSY, LOSSY_REPORT, ORIGINAL, b64, fixture, ino, mode, refuse, run, sha, snapshot, span, wire

REFUSAL = {"error": "strict-loss rejected 2 loss(es)", "report": LOSSY_REPORT}
# All input reads are allowed; a destination lstat must never occur on refusal.
GUARD = {"_lstat": refuse("destination inspected before acceptance")}


def setup(tmp_path, source=LOSSY, options=None):
    path, output = tmp_path / "bundle.json", tmp_path / "source.json"
    bundle = create_bundle(source, options)
    text = wire(bundle)
    path.write_text(text, encoding="utf-8")
    output.write_bytes(b"PRESERVE\r\n")
    os.chmod(output, 0o640)
    return path, output, bundle, text


def success(result):
    assert result.status == 0, result.stderr
    assert result.stdout == "" and result.stderr == ""


def failure(result, code, error):
    assert result.status == code, result.stderr
    assert result.stdout == ""
    assert result.stderr == stringify(error)


def test_strict_extract_refuses_lossy_file_and_stdin_and_preserves_destinations(tmp_path):
    path, output, bundle, text = setup(tmp_path)
    assert bundle["lossReport"] == LOSSY_REPORT and bundle["options"]["strictLoss"] is False
    before = snapshot(tmp_path)
    for source in (path, "-"):
        for target in (output, tmp_path / "absent", path):
            failure(run(["extract", source, "--output", target, "--strict-loss"], text if source == "-" else None), 2, REFUSAL)
            assert snapshot(tmp_path) == before


def test_strict_refusal_precedes_destination_inspection_invalid_types_and_missing_parents(tmp_path):
    path, output, _, text = setup(tmp_path)
    failure(run(["extract", path, "--strict-loss", "--output", output], hooks=GUARD), 2, REFUSAL)
    # Positive control: the guard traps the writer after a normal lossy acceptance.
    failure(run(["extract", path, "--output", output], hooks=GUARD), 1, {"error": "destination inspected before acceptance"})
    link, directory = tmp_path / "link", tmp_path / "directory"
    os.symlink(output, link)
    directory.mkdir()
    for target in (link, directory, tmp_path / "missing" / "source"):
        failure(run(["extract", "-", "--strict-loss", "--output", target], text), 2, REFUSAL)
    assert output.read_bytes() == b"PRESERVE\r\n" and link.is_symlink()
    assert os.listdir(directory) == []
    assert sorted(os.listdir(tmp_path)) == ["bundle.json", "directory", "link", "source.json"]


@pytest.mark.parametrize("strict_loss", [False, True])
def test_lossless_strict_extraction_accepts_zero_and_multiple_spans(tmp_path, strict_loss):
    path, output, _, _ = setup(tmp_path, ORIGINAL)
    sources = [ORIGINAL, b"\xef\xbb\xbf\r\n[]\r\n"]
    sources += [("﻿\r\n" + fixture(f"common.{fmt}.json").replace("\n", "\r\n") + "\r\n").encode()
                for fmt in ("otlp", "jaeger", "zipkin")]
    for data in sources:
        bundle = create_bundle(data, {"strictLoss": strict_loss})
        assert bundle["lossReport"]["losses"] == []
        text = wire(bundle)
        path.write_text(text, encoding="utf-8")
        output.unlink()  # First acceptance creates an absent file; stdin then replaces it.
        for source in (path, "-"):
            success(run(["extract", source, "--strict-loss", "--output", output], text if source == "-" else None))
            assert output.read_bytes() == data and mode(output) == 0o600
            assert path.read_text(encoding="utf-8") == text
            assert sorted(os.listdir(tmp_path)) == ["bundle.json", "source.json"]


def test_accepted_strict_extraction_atomically_replaces_an_existing_inode_and_supports_in_place(tmp_path):
    path, output, _, text = setup(tmp_path, ORIGINAL)
    old_link = tmp_path / "old-inode"
    os.link(output, old_link)
    old_inode = ino(output)
    success(run(["extract", "-", "--output", output, "--strict-loss"], text))
    assert output.read_bytes() == ORIGINAL and mode(output) == 0o600
    assert ino(output) != old_inode and old_link.read_bytes() == b"PRESERVE\r\n"
    success(run(["extract", path, "--strict-loss", "--output", path]))
    assert path.read_bytes() == ORIGINAL and mode(path) == 0o600
    assert sorted(os.listdir(tmp_path)) == ["bundle.json", "old-inode", "source.json"]


def test_default_lossy_extraction_still_restores_every_original_byte_silently(tmp_path):
    path, output, _, text = setup(tmp_path)
    for source in (path, "-"):
        success(run(["extract", source, "--output", output], text if source == "-" else None))
        assert output.read_bytes() == LOSSY and mode(output) == 0o600
    success(run(["extract", path, "--output", path]))
    assert path.read_bytes() == LOSSY
    assert sorted(os.listdir(tmp_path)) == ["bundle.json", "source.json"]


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


def _source_normalized(b):
    data = LOSSY.decode("utf-8").replace("café/雪", "changed", 1).encode("utf-8")
    b["source"]["data"] = b64(data)
    b["sha256"]["source"] = sha(data)


def _source_report(b):
    b["source"]["data"] = b64(ORIGINAL)
    b["sha256"]["source"] = sha(ORIGINAL)


def _recorded_strict(b):
    b["options"]["strictLoss"] = True
    b["sha256"]["options"] = sha(canonical_json(b["options"]))


TAMPERED = [
    ("schema", _delete_normalized, "bundle: missing normalized"),
    ("hash", _source_hash, "bundle.source: SHA-256 mismatch"),
    ("normalized replay", _rehash_normalized, "bundle.normalized: does not match offline conversion replay"),
    ("report replay", _rehash_report, "bundle.lossReport: does not match offline conversion replay"),
    ("source normalized replay", _source_normalized, "bundle.normalized: does not match offline conversion replay"),
    ("source report replay", _source_report, "bundle.lossReport: does not match offline conversion replay"),
    ("recorded strict-loss replay", _recorded_strict,
     "bundle: offline conversion replay failed: strict-loss rejected 2 loss(es)"),
]


@pytest.mark.parametrize("name,mutate,error", TAMPERED, ids=[c[0] for c in TAMPERED])
def test_strict_extract_rejects_tampering_at_exit_1_before_policy_or_destination_access(tmp_path, name, mutate, error):
    path, output, bundle, _ = setup(tmp_path)
    mutate(bundle)
    text = wire(bundle)
    path.write_text(text, encoding="utf-8")
    before = snapshot(tmp_path)
    for target in (output, tmp_path / "absent", path, tmp_path / "missing" / "source"):
        failure(run(["extract", path, "--strict-loss", "--output", target], hooks=GUARD), 1, {"error": error})
        assert snapshot(tmp_path) == before
    failure(run(["extract", "-", "--strict-loss", "--output", output], text), 1, {"error": error})
    assert snapshot(tmp_path) == before


def test_duplicate_strict_loss_and_closed_flags_reject_before_input_io(tmp_path):
    setup(tmp_path)
    missing, output = tmp_path / "missing", tmp_path / "absent"
    before = snapshot(tmp_path)
    for source in (missing, "-"):
        failure(run(["extract", source, "--strict-loss", "--output", output, "--strict-loss"], "invalid JSON"), 1,
                {"error": "duplicate option --strict-loss"})
    for flag in ("--source", "--report", "--from", "--to", "--dry-run", "--zipkin-error-tag", "--strict-loss=true"):
        failure(run(["extract", missing, "--output", output, "--strict-loss", flag]), 1,
                {"error": f"unknown option {flag} for extract"})
    failure(run(["extract", missing, "--strict-loss", "true", "--output", output]), 1,
            {"error": "unknown option true for extract"})
    for extra in ([], ["--output", "-"]):
        failure(run(["extract", missing, "--strict-loss", *extra]), 1,
                {"error": "extract requires --output FILE (a file path, not stdout)"})
    assert snapshot(tmp_path) == before
    help_result = run(["--help"])
    assert help_result.status == 0 and help_result.stderr == ""
    assert "tracepack extract BUNDLE|- --output FILE [--strict-loss]" in help_result.stdout
