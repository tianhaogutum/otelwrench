# SPDX-License-Identifier: Apache-2.0
import json
import re

import pytest

from otelwrench.tracepack import canonical_json, create_bundle, stringify

from .support import LOSSY, LOSSY_REPORT, ORIGINAL, b64, run, sha, snapshot, span, unb64, wire

REFUSAL = {"error": "strict-loss rejected 2 loss(es)", "report": LOSSY_REPORT}


def summary(source, loss_count=0):
    return {"valid": True, "format": "tracepack-fixture", "version": 1, "sourceSha256": sha(source), "spanCount": 1,
            "lossCount": loss_count, "guarantee": "reproducibility-only"}


def setup(tmp_path, source=LOSSY, options=None):
    path, saved = tmp_path / "bundle.json", tmp_path / "source.json"
    bundle = create_bundle(source, options)
    text = wire(bundle)
    path.write_text(text, encoding="utf-8")
    saved.write_bytes(source)
    return path, saved, bundle, text


def failure(result, code, expected):
    assert result.status == code, result.stderr
    assert result.stdout == ""
    if isinstance(expected, dict):
        assert result.stderr == stringify(expected)
    else:
        error = json.loads(result.stderr)
        assert list(error) == ["error"]
        assert re.search(expected, error["error"])


def test_verify_strict_loss_refuses_a_valid_lossy_bundle_with_the_complete_report(tmp_path):
    path, saved, bundle, text = setup(tmp_path)
    assert bundle["options"]["strictLoss"] is False and bundle["lossReport"] == LOSSY_REPORT
    before = snapshot(tmp_path)
    for p in (path, "-"):
        for extra in ([], ["--source", saved]):
            failure(run(["verify", p, "--strict-loss", *extra], text if p == "-" else None), 2, REFUSAL)
    failure(run(["verify", path, "--source", saved, "--strict-loss"]), 2, REFUSAL)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("strict_loss", [False, True])
def test_lossless_verification_succeeds_with_strict_policy(tmp_path, strict_loss):
    path, saved, _, text = setup(tmp_path, ORIGINAL, {"strictLoss": strict_loss})
    before = snapshot(tmp_path)
    for p in (path, "-"):
        for extra in ([], ["--source", saved]):
            result = run(["verify", p, *extra, "--strict-loss"], text if p == "-" else None)
            assert result.status == 0, result.stderr
            assert result.stderr == ""
            assert result.stdout == stringify({**summary(ORIGINAL), **({"sourceMatches": True} if extra else {})})
    assert snapshot(tmp_path) == before


def test_default_verify_still_accepts_lossy_bundles_with_its_exact_previous_summary(tmp_path):
    path, saved, _, _ = setup(tmp_path)
    for extra in ([], ["--source", saved]):
        result = run(["verify", path, *extra])
        assert result.status == 0, result.stderr
        assert result.stderr == ""
        assert result.stdout == stringify({**summary(LOSSY, 2), **({"sourceMatches": True} if extra else {})})


def test_strict_policy_keeps_exact_source_mismatches_and_io_failures_at_exit_1(tmp_path):
    path, saved, bundle, _ = setup(tmp_path)
    changes = [lambda s: s[1:], lambda s: s.replace("\r\n", "\n"), lambda s: s + " \t",
               lambda s: s.replace("9007199254740993", "9.007199254740993e15", 1),
               lambda s: s.replace("9007199254740993", "9007199254740993.0", 1)]
    for change in changes:
        source = change(LOSSY.decode("utf-8")).encode("utf-8")
        other = create_bundle(source)
        assert canonical_json(other["normalized"]) == canonical_json(bundle["normalized"])
        assert other["lossReport"] == LOSSY_REPORT
        saved.write_bytes(source)
        before = snapshot(tmp_path)
        failure(run(["verify", path, "--strict-loss", "--source", saved]), 1,
                {"error": "source does not match embedded original bytes"})
        assert snapshot(tmp_path) == before
    failure(run(["verify", path, "--strict-loss", "--source", tmp_path / "missing"]), 1, "ENOENT")


def _delete_normalized(b):
    del b["normalized"]


def _source_hash(b):
    b["sha256"]["source"] = "0" * 64


def _rehash_normalized(b):
    span(b["normalized"])["name"] = "tampered"
    b["sha256"]["normalized"] = sha(canonical_json(b["normalized"]))


def _removed_losses(b):
    b["lossReport"]["losses"] = []
    b["sha256"]["lossReport"] = sha(canonical_json(b["lossReport"]))


def _changed_source(b):
    b["source"]["data"] = b64(ORIGINAL)
    b["sha256"]["source"] = sha(ORIGINAL)


def _recorded_strict(b):
    b["options"]["strictLoss"] = True
    b["sha256"]["options"] = sha(canonical_json(b["options"]))


TAMPERED = [
    ("schema", _delete_normalized, "missing normalized"),
    ("hash", _source_hash, "source: SHA-256 mismatch"),
    ("normalized replay", _rehash_normalized, "normalized: does not match offline conversion replay"),
    ("removed loss report replay", _removed_losses, "lossReport: does not match offline conversion replay"),
    ("changed source replay", _changed_source, "lossReport: does not match offline conversion replay"),
    ("recorded strict-loss replay", _recorded_strict, r"offline conversion replay failed: strict-loss rejected 2 loss\(es\)"),
]


@pytest.mark.parametrize("name,mutate,expected", TAMPERED, ids=[c[0] for c in TAMPERED])
def test_strict_policy_cannot_bypass_verification_even_with_matching_source(tmp_path, name, mutate, expected):
    path, saved, bundle, _ = setup(tmp_path)
    mutate(bundle)
    path.write_text(wire(bundle), encoding="utf-8")
    saved.write_bytes(unb64(bundle["source"]["data"]))
    before = snapshot(tmp_path)
    for source in (saved, tmp_path / "missing"):
        failure(run(["verify", path, "--strict-loss", "--source", source]), 1, expected)
    assert snapshot(tmp_path) == before


def test_strict_policy_rejects_duplicate_flags_and_retains_argument_boundaries():
    failure(run(["verify", "missing.json", "--strict-loss", "--strict-loss"]), 1, r"^duplicate option --strict-loss$")
    for flag in ("--output", "--from", "--dry-run", "--zipkin-error-tag"):
        failure(run(["verify", "missing.json", "--strict-loss", flag]), 1, f"unknown option {flag} for verify")
    failure(run(["extract", "missing.json", "--strict-loss"]), 1,
            r"^extract requires --output FILE \(a file path, not stdout\)$")
    help_result = run(["--help"])
    assert help_result.status == 0 and help_result.stderr == ""
    assert re.search(r"tracepack verify BUNDLE\|- \[--source SOURCE\] \[--strict-loss\]", help_result.stdout)
