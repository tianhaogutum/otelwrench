# SPDX-License-Identifier: Apache-2.0
import math

import pytest

from otelwrench.tracepack import (UNDEFINED, StrictLossError, TracepackError, canonical_json, create_bundle,
                                  extract_bundle, verify_bundle)

from .support import LOSSY, LOSSY_REPORT, ORIGINAL, b64, fixture, sha, span, wire

TEXT = wire(create_bundle(LOSSY))


def ordinary_error(call, message):
    with pytest.raises(TracepackError) as info:
        call()
    assert type(info.value) is TracepackError and str(info.value) == message
    assert not hasattr(info.value, "report")


def policy_error(call):
    with pytest.raises(StrictLossError) as info:
        call()
    assert str(info.value) == "strict-loss rejected 2 loss(es)"
    assert info.value.code == "STRICT_LOSS" and info.value.report == LOSSY_REPORT
    return info.value


def exact_bytes(serialized, options, expected):
    result = extract_bundle(serialized, options)
    assert isinstance(result, bytes) and result == expected


class Observed(dict):
    def __init__(self, values, on_read):
        super().__init__(values)
        self.on_read = on_read

    def __getitem__(self, key):
        return self.on_read(key, dict.__getitem__(self, key))


class Untouchable:
    def __getattribute__(self, name):
        raise AssertionError("options inspected before full verification")


def test_strict_extraction_refuses_verified_losses_without_returning_bytes():
    result = "not returned"
    with pytest.raises(StrictLossError):
        result = extract_bundle(TEXT, {"strictLoss": True})
    assert result == "not returned"
    policy_error(lambda: extract_bundle(TEXT, {"strictLoss": True}))


@pytest.mark.parametrize("name,data", [("lossless", ORIGINAL), ("lossy", LOSSY)])
def test_omitted_undefined_and_false_policy_preserve_exact_original_bytes(name, data):
    bundle = create_bundle(data)
    serialized, before = wire(bundle), bytes(data)
    assert bundle["options"]["strictLoss"] is False
    for options in (UNDEFINED, {}, {"strictLoss": UNDEFINED}, {"strictLoss": False}):
        exact_bytes(serialized, options, data)
    assert extract_bundle(serialized) == data
    assert data == before and wire(bundle) == serialized


@pytest.mark.parametrize("strict_loss", [False, True])
def test_lossless_strict_extraction_accepts_zero_and_multiple_spans(strict_loss):
    for source in (ORIGINAL, b"\xef\xbb\xbf\r\n[]\r\n"):
        exact_bytes(wire(create_bundle(source, {"strictLoss": strict_loss})), {"strictLoss": True}, source)
    for fmt in ("otlp", "jaeger", "zipkin"):
        source = fixture(f"common.{fmt}.json").encode()
        bundle = create_bundle(source, {"from": fmt, "strictLoss": strict_loss})
        assert bundle["lossReport"]["spanCount"] == 2 and bundle["lossReport"]["losses"] == []
        exact_bytes(wire(bundle), {"strictLoss": True}, source)


def _refuse_strict(key, value):
    if key == "strictLoss":
        raise AssertionError("strictLoss read before keys validated")
    return value


def test_defined_strict_loss_accepts_only_booleans_and_invalid_options_precede_policy():
    for strict_loss in (None, 0, 1, math.nan, "", "false", "true", [], {}, lambda: None, 2**70):
        ordinary_error(lambda v=strict_loss: extract_bundle(TEXT, {"strictLoss": v}), "options.strictLoss: expected boolean")
    for options in (None, [], True, False, "options", 0, 2**70, lambda: None):
        ordinary_error(lambda options=options: extract_bundle(TEXT, options), "options: expected object")
    for key in ("source", "report", "typo"):
        options = Observed({key: True, "strictLoss": None}, _refuse_strict)
        ordinary_error(lambda options=options: extract_bundle(TEXT, options), f"options: unexpected {key}")


def test_strict_loss_value_is_read_once_after_replay_for_acceptance_and_refusal():
    for source, strict_loss in ((ORIGINAL, True), (LOSSY, False), (LOSSY, UNDEFINED), (LOSSY, True)):
        reads = []

        def record(key, value, reads=reads, strict_loss=strict_loss):
            reads.append(key)
            return strict_loss
        options = Observed({"strictLoss": None}, record)
        serialized = wire(create_bundle(source))
        if source == LOSSY and strict_loss is True:
            policy_error(lambda: extract_bundle(serialized, options))
        else:
            exact_bytes(serialized, options, source)
        assert reads == ["strictLoss"]


def test_policy_errors_and_returned_bytes_are_independent_across_calls():
    bundle = create_bundle(LOSSY)
    before = wire(bundle)
    error = policy_error(lambda: extract_bundle(before, {"strictLoss": True}))
    error.report["losses"].pop()
    error.report["from"] = "changed"
    policy_error(lambda: extract_bundle(before, {"strictLoss": True}))
    result = bytearray(extract_bundle(before, {"strictLoss": False}))
    result[:] = bytes(len(result))
    exact_bytes(before, {"strictLoss": False}, LOSSY)
    assert wire(bundle) == before
    summary = {"valid": True, "format": "tracepack-fixture", "version": 1, "sourceSha256": sha(LOSSY), "spanCount": 1,
               "lossCount": 2, "guarantee": "reproducibility-only"}
    assert verify_bundle(before) == summary
    assert verify_bundle(before, {"report": True, "source": LOSSY}) == {**summary, "sourceMatches": True,
                                                                         "report": LOSSY_REPORT}
    policy_error(lambda: verify_bundle(before, {"strictLoss": True, "source": LOSSY, "report": True}))


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


@pytest.mark.parametrize("name,mutate,message", TAMPERED, ids=[c[0] for c in TAMPERED])
def test_full_verification_rejects_tampering_before_options_inspection(name, mutate, message):
    bundle = create_bundle(LOSSY)
    mutate(bundle)
    serialized = wire(bundle)
    for options in (Untouchable(), None, {"strictLoss": "true"}, {"strictLoss": True}, {"strictLoss": False}):
        ordinary_error(lambda options=options: extract_bundle(serialized, options), message)


def test_bundle_type_validation_precedes_invalid_options_and_strict_policy():
    for value in (None, UNDEFINED, TEXT.encode(), {}, [], 1):
        for options in (None, {"strictLoss": True}):
            ordinary_error(lambda value=value, options=options: extract_bundle(value, options), "bundle: expected JSON text")
