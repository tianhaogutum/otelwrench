# SPDX-License-Identifier: Apache-2.0
import array
import re

import pytest

from otelwrench.tracepack import (UNDEFINED, StrictLossError, TracepackError, canonical_json, create_bundle,
                                  extract_bundle, verify_bundle)

from .support import LOSSY, LOSSY_REPORT, ORIGINAL, SOURCE_LIMIT, sha, span, unb64, b64, wire

TEXT = wire(create_bundle(LOSSY))


def summary(source, loss_count=0):
    return {"valid": True, "format": "tracepack-fixture", "version": 1, "sourceSha256": sha(source), "spanCount": 1,
            "lossCount": loss_count, "guarantee": "reproducibility-only"}


def policy_error(call):
    with pytest.raises(StrictLossError) as info:
        call()
    assert str(info.value) == "strict-loss rejected 2 loss(es)"
    assert info.value.code == "STRICT_LOSS" and info.value.report == LOSSY_REPORT


def ordinary_error(call, pattern):
    with pytest.raises(TracepackError) as info:
        call()
    assert type(info.value) is TracepackError
    assert re.search(pattern, str(info.value))
    assert getattr(info.value, "code", None) is None and getattr(info.value, "report", None) is None


class Observed(dict):
    def __init__(self, values, on_read):
        super().__init__(values)
        self.on_read = on_read

    def __getitem__(self, key):
        return self.on_read(key, dict.__getitem__(self, key))


def test_library_strict_policy_refuses_verified_losses_after_exact_source_equality():
    bundle = create_bundle(LOSSY)
    assert bundle["options"]["strictLoss"] is False and bundle["lossReport"] == LOSSY_REPORT
    for source in (UNDEFINED, LOSSY, bytearray(LOSSY)):
        policy_error(lambda source=source: verify_bundle(TEXT, {"strictLoss": True, "source": source}))
    # Neither a policy error nor extraction changes the recorded bundle or original bytes.
    assert wire(bundle) == TEXT and extract_bundle(TEXT) == LOSSY
    assert verify_bundle(TEXT) == summary(LOSSY, 2)


def test_omitted_undefined_and_false_strict_loss_retain_the_exact_previous_lossy_summaries():
    assert verify_bundle(TEXT) == summary(LOSSY, 2)
    for options in ({}, {"strictLoss": UNDEFINED}, {"strictLoss": False}):
        assert verify_bundle(TEXT, options) == summary(LOSSY, 2)
        assert verify_bundle(TEXT, {**options, "source": LOSSY}) == {**summary(LOSSY, 2), "sourceMatches": True}


@pytest.mark.parametrize("strict_loss", [False, True])
def test_lossless_strict_library_policy_preserves_success(strict_loss):
    serialized = wire(create_bundle(ORIGINAL, {"strictLoss": strict_loss}))
    assert verify_bundle(serialized, {"strictLoss": True}) == summary(ORIGINAL)
    assert verify_bundle(serialized, {"strictLoss": True, "source": ORIGINAL}) == {**summary(ORIGINAL), "sourceMatches": True}
    empty = wire(create_bundle("[]", {"strictLoss": strict_loss}))
    assert verify_bundle(empty, {"strictLoss": True}) == {**summary(b"[]"), "spanCount": 0}


def _refuse_source(key, value):
    if key == "source":
        raise AssertionError("source read before option validation")
    return value


def test_library_strict_loss_is_boolean_rejects_unknown_options_and_reads_its_value_once():
    for strict_loss in (None, 0, 1, "", "true", [], {}, lambda: None):
        options = Observed({"strictLoss": strict_loss, "source": None}, _refuse_source)
        ordinary_error(lambda options=options: verify_bundle(TEXT, options), r"^options.strictLoss: expected boolean$")
    ordinary_error(lambda: verify_bundle(TEXT, {"strictLoss": True, "typo": False}), r"^options: unexpected typo$")
    counts = {"strictLoss": 0, "source": 0}

    def record(key, value):
        counts[key] += 1
        return True if key == "strictLoss" else LOSSY
    policy_error(lambda: verify_bundle(TEXT, Observed({"strictLoss": None, "source": None}, record)))
    assert counts == {"strictLoss": 1, "source": 1}


def test_strict_policy_retains_source_view_offset_and_length_and_never_mutates_caller_bytes():
    allocation = bytearray(b"prefix" + LOSSY + b"suffix")
    before = bytes(allocation)
    for source in (memoryview(allocation)[6:-6], bytes(allocation[6:6 + len(LOSSY)])):
        policy_error(lambda source=source: verify_bundle(TEXT, {"strictLoss": True, "source": source}))
    for source in (allocation, memoryview(allocation)[5:-7], memoryview(allocation)[6:-7]):
        ordinary_error(lambda source=source: verify_bundle(TEXT, {"strictLoss": True, "source": source}),
                       r"^source does not match embedded original bytes$")
    assert bytes(allocation) == before


def test_source_mismatch_type_and_size_errors_precede_strict_refusal():
    changes = [lambda s: s[1:], lambda s: s.replace("\r\n", "\n"), lambda s: s + " \t",
               lambda s: s.replace("9007199254740993", "9.007199254740993e15", 1),
               lambda s: s.replace("9007199254740993", "9007199254740993.0", 1)]
    for change in changes:
        source = change(LOSSY.decode("utf-8")).encode("utf-8")
        other = create_bundle(source)
        assert canonical_json(other["normalized"]) == canonical_json(create_bundle(LOSSY)["normalized"])
        assert other["lossReport"] == LOSSY_REPORT
        ordinary_error(lambda source=source: verify_bundle(TEXT, {"strictLoss": True, "source": source}),
                       r"^source does not match embedded original bytes$")
    for source in (None, LOSSY.decode(), list(LOSSY), array.array("H", [1])):
        ordinary_error(lambda source=source: verify_bundle(TEXT, {"strictLoss": True, "source": source}),
                       r"^options.source: expected Uint8Array$")
    ordinary_error(lambda: verify_bundle(TEXT, {"strictLoss": True, "source": bytearray(SOURCE_LIMIT + 1)}),
                   r"^options.source: input exceeds 64 MiB limit$")


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
    ("schema", _delete_normalized, r"^bundle: missing normalized$"),
    ("hash", _source_hash, r"^bundle.source: SHA-256 mismatch$"),
    ("normalized replay", _rehash_normalized, r"^bundle.normalized: does not match offline conversion replay$"),
    ("removed loss report replay", _removed_losses, r"^bundle.lossReport: does not match offline conversion replay$"),
    ("changed source replay", _changed_source, r"^bundle.lossReport: does not match offline conversion replay$"),
    ("recorded strict-loss replay", _recorded_strict,
     r"^bundle: offline conversion replay failed: strict-loss rejected 2 loss\(es\)$"),
]


def _refuse_all(key, value):
    raise AssertionError(f"{key} accessed before full verification")


@pytest.mark.parametrize("name,mutate,expected", TAMPERED, ids=[c[0] for c in TAMPERED])
def test_library_strict_policy_cannot_bypass_complete_replay_before_options_access(name, mutate, expected):
    bundle = create_bundle(LOSSY)
    mutate(bundle)
    serialized, source = wire(bundle), unb64(bundle["source"]["data"])
    guarded = Observed({"strictLoss": None, "source": None}, _refuse_all)
    for options in (guarded, None, {"strictLoss": True, "source": source}, {"strictLoss": "true", "source": None}):
        ordinary_error(lambda options=options: verify_bundle(serialized, options), expected)
