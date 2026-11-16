# SPDX-License-Identifier: Apache-2.0
import array

import pytest

from otelwrench.tracepack import UNDEFINED, TracepackError, canonical_json, create_bundle, extract_bundle, verify_bundle

from .support import ORIGINAL, SOURCE_LIMIT, b64, fixture, sha, span, unb64, wire

TEXT = wire(create_bundle(ORIGINAL))
SUMMARY = {"valid": True, "format": "tracepack-fixture", "version": 1, "sourceSha256": sha(ORIGINAL), "spanCount": 1,
           "lossCount": 0, "guarantee": "reproducibility-only"}
MISMATCH = "source does not match embedded original bytes"


def assert_mismatch(call):
    with pytest.raises(TracepackError) as info:
        call()
    assert type(info.value) is TracepackError and str(info.value) == MISMATCH


def test_library_source_comparison_is_opt_in_and_preserves_the_plain_summary():
    assert verify_bundle(TEXT) == SUMMARY
    for options in ({}, {"source": UNDEFINED}):
        assert verify_bundle(TEXT, options) == SUMMARY
    assert extract_bundle(TEXT) == ORIGINAL
    checked = verify_bundle(TEXT, {"source": ORIGINAL})
    assert checked == {**SUMMARY, "sourceMatches": True}
    checked["sourceMatches"] = False
    assert verify_bundle(TEXT) == SUMMARY


def test_library_accepts_exact_bytes_views_with_offsets_and_never_mutates_caller_bytes():
    allocation = bytearray(b"prefix" + ORIGINAL + b"suffix")
    before = bytes(allocation)
    views = [ORIGINAL, bytearray(ORIGINAL), memoryview(allocation)[6:-6], bytes(allocation[6:6 + len(ORIGINAL)])]
    for source in views:
        saved = bytes(source)
        assert verify_bundle(TEXT, {"source": source}) == {**SUMMARY, "sourceMatches": True}
        assert bytes(source) == saved
    # The underlying allocation contains the right source, but these selected views do not.
    for source in (allocation, memoryview(allocation)[5:-7], memoryview(allocation)[6:-7]):
        assert_mismatch(lambda source=source: verify_bundle(TEXT, {"source": source}))
    assert bytes(allocation) == before


CHANGES = [
    ("BOM removal", lambda s: s[1:]),
    ("CRLF to LF", lambda s: s.replace("\r\n", "\n")),
    ("whitespace only", lambda s: s + " \t"),
    ("unsafe exponent spelling", lambda s: s.replace("9007199254740993", "9.007199254740993e15", 1)),
    ("unsafe decimal spelling", lambda s: s.replace("9007199254740993", "9007199254740993.0", 1)),
]


@pytest.mark.parametrize("name,change", CHANGES, ids=[c[0] for c in CHANGES])
def test_library_rejects_changed_bytes_even_with_identical_normalization(name, change):
    source = change(ORIGINAL.decode("utf-8")).encode("utf-8")
    other, bundle = create_bundle(source), create_bundle(ORIGINAL)
    assert canonical_json(other["normalized"]) == canonical_json(bundle["normalized"])
    assert other["lossReport"] == bundle["lossReport"]
    assert_mismatch(lambda: verify_bundle(TEXT, {"source": source}))


@pytest.mark.parametrize("fmt", ["otlp", "jaeger", "zipkin"])
def test_library_compares_exact_bytes_including_unsupported_original_fields(fmt):
    raw = fixture(f"common.{fmt}.json")
    source = (raw.replace('"name":', '"debug":true,"name":', 1) if fmt == "zipkin" else raw).encode()
    serialized = wire(create_bundle(source))
    checked = verify_bundle(serialized, {"source": bytearray(source)})
    assert checked["sourceMatches"] is True and checked["spanCount"] == 2
    assert checked["lossCount"] == (1 if fmt == "zipkin" else 0)
    assert extract_bundle(serialized) == source
    assert_mismatch(lambda: verify_bundle(serialized, {"source": b""}))
    if fmt == "zipkin":
        assert_mismatch(lambda: verify_bundle(serialized, {"source": raw.encode()}))


def test_library_rejects_invalid_options_source_types_unknown_options_and_oversized_source():
    for options in (None, [], "path.json", 1, False, lambda: None):
        with pytest.raises(TracepackError, match=r"^options: expected object$"):
            verify_bundle(TEXT, options)
    with pytest.raises(TracepackError, match=r"^options: unexpected soruce$"):
        verify_bundle(TEXT, {"soruce": ORIGINAL})
    for source in (None, ORIGINAL.decode("utf-8"), list(ORIGINAL), array.array("H", [1]), {}, True, 1):
        with pytest.raises(TracepackError, match=r"^options.source: expected Uint8Array$"):
            verify_bundle(TEXT, {"source": source})
    with pytest.raises(TracepackError, match=r"^options.source: input exceeds 64 MiB limit$"):
        verify_bundle(TEXT, {"source": bytearray(SOURCE_LIMIT + 1)})


class Guarded(dict):
    """Options mapping that records or refuses value reads."""

    def __init__(self, values, on_read):
        super().__init__(values)
        self.on_read = on_read

    def __getitem__(self, key):
        return self.on_read(key, dict.__getitem__(self, key))


def _refuse(key, value):
    raise AssertionError(f"{key} accessed before verification completed")


def _version(b):
    b["version"] = 2


def _missing_report(b):
    del b["lossReport"]


def _source_hash(b):
    b["sha256"]["source"] = "0" * 64


def _rehash_normalized(b):
    span(b["normalized"])["name"] = "tampered"
    b["sha256"]["normalized"] = sha(canonical_json(b["normalized"]))


def test_library_fully_verifies_before_reading_options_or_source():
    cases = [(_version, "bundle.version"), (_missing_report, "missing lossReport"),
             (_source_hash, "bundle.source: SHA-256 mismatch"),
             (_rehash_normalized, "bundle.normalized: does not match offline conversion replay")]
    for mutate, expected in cases:
        bundle = create_bundle(ORIGINAL)
        mutate(bundle)
        for options in (Guarded({"source": None}, _refuse), None, {"source": ORIGINAL}):
            with pytest.raises(TracepackError, match=expected):
                verify_bundle(wire(bundle), options)
    reads = []

    def record(key, value):
        reads.append(key)
        return ORIGINAL
    assert verify_bundle(TEXT, Guarded({"source": None}, record))["sourceMatches"] is True
    assert reads == ["source"]


@pytest.mark.parametrize("member", ["source", "normalized", "lossReport"])
def test_matching_library_source_cannot_bypass_rehashed_inconsistent_member(member):
    bundle = create_bundle(ORIGINAL)
    if member == "source":
        bundle["source"]["data"] = b64(b"[]")
    if member == "normalized":
        span(bundle["normalized"])["name"] = "tampered"
    if member == "lossReport":
        bundle["lossReport"]["spanCount"] = 999
    source = unb64(bundle["source"]["data"])
    bundle["sha256"][member] = sha(source if member == "source" else canonical_json(bundle[member]))
    with pytest.raises(TracepackError, match="does not match offline conversion replay"):
        verify_bundle(wire(bundle), {"source": source})
