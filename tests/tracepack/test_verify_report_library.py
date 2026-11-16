# SPDX-License-Identifier: Apache-2.0
import array
import math

import pytest

from otelwrench.tracepack import (UNDEFINED, StrictLossError, TracepackError, canonical_json, create_bundle,
                                  extract_bundle, verify_bundle)

from .support import LOSSY, LOSSY_REPORT, ORIGINAL, SOURCE_LIMIT, b64, fixture, sha, span, unb64, wire

LOSSLESS_REPORT = {"from": "zipkin", "to": "otlp", "spanCount": 1, "losses": []}
TEXT = wire(create_bundle(LOSSY))


def summary(source, loss_count=0, span_count=1):
    return {"valid": True, "format": "tracepack-fixture", "version": 1, "sourceSha256": sha(source),
            "spanCount": span_count, "lossCount": loss_count, "guarantee": "reproducibility-only"}


def ordinary_error(call, message):
    with pytest.raises(TracepackError) as info:
        call()
    assert type(info.value) is TracepackError
    assert str(info.value) == message
    assert not hasattr(info.value, "report") and getattr(info.value, "code", None) is None


def policy_error(call):
    with pytest.raises(StrictLossError) as info:
        call()
    assert str(info.value) == "strict-loss rejected 2 loss(es)"
    assert info.value.code == "STRICT_LOSS" and info.value.report == LOSSY_REPORT
    return info.value


class Observed(dict):
    def __init__(self, values, on_read):
        super().__init__(values)
        self.on_read = on_read

    def __getitem__(self, key):
        return self.on_read(key, dict.__getitem__(self, key))


class Untouchable:
    """Fails on any inspection, like the original Proxy guard."""

    def __getattribute__(self, name):
        raise AssertionError("options inspected before verification")


@pytest.mark.parametrize("name,source,expected_report", [("lossless", ORIGINAL, LOSSLESS_REPORT), ("lossy", LOSSY, LOSSY_REPORT)])
def test_library_report_true_appends_the_complete_replay_report(name, source, expected_report):
    serialized = wire(create_bundle(source))
    for strict_loss in (UNDEFINED, False, *([True] if name == "lossless" else [])):
        checked = verify_bundle(serialized, {"report": True, "strictLoss": strict_loss})
        assert checked == {**summary(source, len(expected_report["losses"])), "report": expected_report}
        assert list(checked) == [*summary(source), "report"]
        assert verify_bundle(serialized, {"source": source, "strictLoss": strict_loss, "report": True}) == \
            {**summary(source, len(expected_report["losses"])), "sourceMatches": True, "report": expected_report}
    assert extract_bundle(serialized) == source


def test_omitted_undefined_and_false_report_retain_exact_prior_summaries():
    for source in (ORIGINAL, LOSSY):
        serialized, expected = wire(create_bundle(source)), summary(source, 2 if source == LOSSY else 0)
        assert verify_bundle(serialized) == expected
        assert verify_bundle(serialized, {"source": source}) == {**expected, "sourceMatches": True}
        for options in ({}, {"report": UNDEFINED}, {"report": False}):
            assert verify_bundle(serialized, options) == expected
            assert verify_bundle(serialized, {**options, "source": source}) == {**expected, "sourceMatches": True}


@pytest.mark.parametrize("fmt", ["otlp", "jaeger", "zipkin"])
def test_library_report_preserves_replay_format_and_multiple_spans(fmt):
    source = fixture(f"common.{fmt}.json").encode()
    serialized = wire(create_bundle(source, {"from": fmt, "strictLoss": True}))
    assert verify_bundle(serialized, {"report": True, "strictLoss": True, "source": source}) == {
        **summary(source, 0, 2), "sourceMatches": True, "report": {"from": fmt, "to": "otlp", "spanCount": 2, "losses": []}}


def test_library_report_includes_empty_losses_and_zero_spans():
    source = b"[]"
    for strict_loss in (False, True):
        serialized = wire(create_bundle(source, {"strictLoss": strict_loss}))
        assert verify_bundle(serialized, {"report": True, "strictLoss": True}) == {
            **summary(source, 0, 0), "report": {"from": "zipkin", "to": "otlp", "spanCount": 0, "losses": []}}


def _refuse_source(key, value):
    if key == "source":
        raise AssertionError("source read before report validation")
    return value


def test_only_defined_boolean_report_accepted_and_validation_precedes_source_and_policy():
    for value in (None, 0, 1, math.nan, "", "false", "true", [], {}, lambda: None, 2**70):
        options = Observed({"strictLoss": True, "report": value, "source": None}, _refuse_source)
        ordinary_error(lambda options=options: verify_bundle(TEXT, options), "options.report: expected boolean")
    ordinary_error(lambda: verify_bundle(TEXT, {"strictLoss": "true", "report": True}), "options.strictLoss: expected boolean")
    ordinary_error(lambda: verify_bundle(TEXT, {"report": True, "typo": False}), "options: unexpected typo")
    for options in (None, [], True, "report", 1, lambda: None):
        ordinary_error(lambda options=options: verify_bundle(TEXT, options), "options: expected object")


def test_options_read_once_in_validation_then_source_order():
    for value in (UNDEFINED, False, True):
        reads = []

        def record(key, stored, reads=reads, value=value):
            reads.append(key)
            return {"strictLoss": True, "report": value, "source": LOSSY}[key]
        options = Observed({"strictLoss": None, "report": None, "source": None}, record)
        policy_error(lambda options=options: verify_bundle(TEXT, options))
        assert reads == ["strictLoss", "report", "source"]
    reads = []

    def report_only(key, stored):
        reads.append(key)
        return True
    assert verify_bundle(TEXT, Observed({"report": None}, report_only)) == {**summary(LOSSY, 2), "report": LOSSY_REPORT}
    assert reads == ["report"]


def test_report_composes_with_exact_source_view_offsets_without_shared_returned_state():
    allocation = bytearray(b"prefix" + LOSSY + b"suffix")
    before = bytes(allocation)
    for source in (memoryview(allocation)[6:-6], bytes(allocation[6:6 + len(LOSSY)])):
        checked = verify_bundle(TEXT, {"report": True, "source": source})
        assert checked == {**summary(LOSSY, 2), "sourceMatches": True, "report": LOSSY_REPORT}
        checked["report"]["losses"].pop()
        checked["report"]["from"] = "changed"
        assert verify_bundle(TEXT, {"report": True})["report"] == LOSSY_REPORT
        policy_error(lambda source=source: verify_bundle(TEXT, {"report": True, "source": source, "strictLoss": True}))
    for source in (allocation, memoryview(allocation)[5:-7], memoryview(allocation)[6:-7], LOSSY + b" "):
        ordinary_error(lambda source=source: verify_bundle(TEXT, {"report": True, "source": source, "strictLoss": True}),
                       "source does not match embedded original bytes")
    assert bytes(allocation) == before and extract_bundle(TEXT) == LOSSY


def test_report_preserves_source_mismatch_type_and_size_precedence_over_strict_refusal():
    for source in (None, LOSSY.decode(), list(LOSSY), array.array("H", [1])):
        ordinary_error(lambda source=source: verify_bundle(TEXT, {"report": True, "strictLoss": True, "source": source}),
                       "options.source: expected Uint8Array")
    ordinary_error(lambda: verify_bundle(TEXT, {"report": True, "strictLoss": True, "source": bytearray(SOURCE_LIMIT + 1)}),
                   "options.source: input exceeds 64 MiB limit")


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


def _source_replay(b):
    b["source"]["data"] = b64(ORIGINAL)
    b["sha256"]["source"] = sha(ORIGINAL)


def _recorded_policy(b):
    b["options"]["strictLoss"] = True
    b["sha256"]["options"] = sha(canonical_json(b["options"]))


TAMPERED = [
    ("schema", _delete_normalized, "bundle: missing normalized"),
    ("hash", _source_hash, "bundle.source: SHA-256 mismatch"),
    ("normalized replay", _rehash_normalized, "bundle.normalized: does not match offline conversion replay"),
    ("report replay", _rehash_report, "bundle.lossReport: does not match offline conversion replay"),
    ("source replay", _source_replay, "bundle.lossReport: does not match offline conversion replay"),
    ("recorded policy replay", _recorded_policy,
     "bundle: offline conversion replay failed: strict-loss rejected 2 loss(es)"),
]


@pytest.mark.parametrize("name,mutate,message", TAMPERED, ids=[c[0] for c in TAMPERED])
def test_library_report_cannot_bypass_full_verification_before_options_access(name, mutate, message):
    bundle = create_bundle(LOSSY)
    mutate(bundle)
    serialized, source = wire(bundle), unb64(bundle["source"]["data"])
    for options in (Untouchable(), None, {"report": True, "strictLoss": True, "source": source},
                    {"report": "true", "source": None}):
        ordinary_error(lambda options=options: verify_bundle(serialized, options), message)
