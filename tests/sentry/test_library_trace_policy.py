"""Direct conversion shares the CLI's opt-in, post-validation trace policy."""
from copy import deepcopy
import gzip
import hashlib
import json
import unittest

from otelwrench.sentry.mapping import convert_bytes
from otelwrench.sentry.parser import ConversionError
from .test_consistency import OTHER, TRACE, source_for, transaction
from .test_direct_bytes import SUBCLASSES
from .test_parser import envelope
from .verify_output import verify_documents


class LibraryTracePolicyTests(unittest.TestCase):
    def inputs(self, payload):
        for compression in ("none", "gzip"):
            source = gzip.compress(payload, mtime=0) if compression == "gzip" else payload
            options = dict(input_compression=compression, max_input_bytes=len(source),
                           max_decompressed_bytes=len(payload))
            for kind in (bytes, *SUBCLASSES):
                yield kind(source), options, source

    def test_conflicts_cycles_and_both_reject_with_complete_counts(self):
        duplicate = [transaction("3"), {**transaction("3"), "release": "changed"}, transaction("3")]
        cycle = [transaction("2", "1"), transaction("1", "2"), transaction("1", "2")]
        # An ambiguous second cycle is excluded; its conflicting identity still counts.
        ambiguous = [transaction("4", "5"), transaction("5", "4"), transaction("5", "9")]
        for events, conflicts, cycles in ((duplicate, 1, 0), (cycle, 0, 1),
                                         (duplicate + cycle + ambiguous, 2, 1)):
            payload = source_for({"message": "earlier valid log"}, *events)
            for data, options, source in self.inputs(payload):
                with self.subTest(kind=type(data), options=options, counts=(conflicts, cycles)):
                    default = convert_bytes(data, **options)
                    self.assertEqual(convert_bytes(data, fail_on_trace_conflicts=False, **options), default)
                    summary = default["manifest.json"]["traceConsistency"]["summary"]
                    self.assertEqual((summary["conflictingDuplicateGroups"], summary["cycles"]),
                                     (conflicts, cycles))
                    with self.assertRaises(ConversionError) as caught:
                        convert_bytes(data, fail_on_trace_conflicts=True, **options)
                    self.assertEqual(str(caught.exception),
                                     f"trace conflicts: conflicting duplicate groups={conflicts}, confirmed cycles={cycles}")

    def test_allowed_diagnostics_preserve_documents_actual_extent_and_provenance(self):
        one = transaction("1")
        normalized = deepcopy(one)
        normalized["contexts"]["trace"] = {"trace_id": "A" * 32, "span_id": "1" * 16}
        normalized["start_timestamp"] = "1970-01-01T00:00:01Z"
        normalized["ignored"] = "discarded fields do not define conflicts"
        payload = source_for(one, normalized, transaction("1", trace=OTHER), transaction("2", "1"),
                             transaction("3", "9"), transaction("4", "3", trace=OTHER),
                             {"message": "uncorrelated"})
        for data, options, source in self.inputs(payload):
            with self.subTest(kind=type(data), options=options):
                expected = convert_bytes(source, **options)
                actual = convert_bytes(data, fail_on_trace_conflicts=True, **options)
                self.assertEqual(actual, expected)
                self.assertEqual(verify_documents(actual, source),
                                 {"items": 7, "logs": 1, "spans": 6, "losses": 1})
                manifest = actual["manifest.json"]
                self.assertEqual(manifest["input"]["bytes"], len(source))
                self.assertEqual(manifest["input"]["sha256"], hashlib.sha256(source).hexdigest())
                summary = manifest["traceConsistency"]["summary"]
                self.assertEqual(summary["identicalDuplicateGroups"], 1)
                self.assertEqual(summary["parents"], {"not_supplied": 3, "resolved": 1, "ambiguous": 0,
                                                     "missing": 1, "other_trace_only": 1})
                self.assertEqual(summary["uncorrelatedLogs"], 1)

    def test_standalone_json_empty_envelope_and_explicit_skips_accept(self):
        for payload, extra in (
                (b'{"message":"standalone"}', {"input_format": "json"}),
                (json.dumps(transaction("1")).encode(), {"input_format": "json"}),
                (b"{}\n", {"input_format": "envelope"}),
                (envelope(b"\xffopaque", kind="attachment"), {"skip_unsupported": True})):
            for data, options, source in self.inputs(payload):
                with self.subTest(kind=type(data), options=options, extra=extra):
                    self.assertEqual(convert_bytes(data, fail_on_trace_conflicts=True, **options, **extra),
                                     convert_bytes(source, **options, **extra))
        # Explicit skips cannot hide trace conflicts in other items.
        payload = source_for(transaction("1"), {**transaction("1"), "timestamp": 3})
        payload += envelope(b"\xffopaque", kind="attachment")[3:]
        for data, options, source in self.inputs(payload):
            with self.subTest(kind=type(data), options=options), self.assertRaisesRegex(
                    ConversionError, "^trace conflicts: conflicting duplicate groups=1, confirmed cycles=0$"):
                convert_bytes(data, skip_unsupported=True, fail_on_trace_conflicts=True, **options)

    def test_later_mapping_framing_and_unsupported_errors_precede_trace_policy(self):
        conflict = source_for(transaction("1"), {**transaction("1"), "transaction": "changed"})
        bad = {**transaction("2"), "timestamp": 0}
        for suffix, extra, diagnostic in (
                (source_for(bad)[3:], {"skip_unsupported": True}, "end precedes start"),
                (envelope(b'{"message":', kind="event")[3:], {}, "invalid JSON/UTF-8"),
                (b"invalid header\n{}", {}, "item 2 header"),
                (envelope(b"\xffopaque", kind="attachment")[3:], {}, "unsupported item type")):
            for data, options, source in self.inputs(conflict + suffix):
                with self.subTest(kind=type(data), diagnostic=diagnostic, options=options):
                    with self.assertRaises(ConversionError) as default:
                        convert_bytes(source, **options, **extra)
                    self.assertIn(diagnostic, str(default.exception))
                    with self.assertRaises(ConversionError) as strict:
                        convert_bytes(data, fail_on_trace_conflicts=True, **options, **extra)
                    self.assertEqual(str(strict.exception), str(default.exception))
                    self.assertNotIn("trace conflicts:", str(strict.exception))

    def test_gzip_trailers_and_expansion_precede_trace_policy(self):
        payload = source_for(transaction("1"), {**transaction("1"), "release": "changed"})
        source = gzip.compress(payload, mtime=0)
        corrupt = bytearray(source)
        corrupt[-8] ^= 1
        bad_later_member = gzip.compress(b"", mtime=0)[:-1]
        for data, limit, diagnostic in ((source[:-1], len(payload), "invalid or truncated gzip"),
                                        (bytes(corrupt), len(payload), "invalid or truncated gzip"),
                                        (source + bad_later_member, len(payload), "invalid or truncated gzip"),
                                        (source, len(payload) - 1, "gzip expansion exceeds")):
            options = dict(input_compression="gzip", max_decompressed_bytes=limit)
            for kind in (bytes, *SUBCLASSES):
                with self.subTest(kind=kind, diagnostic=diagnostic):
                    with self.assertRaises(ConversionError) as default:
                        convert_bytes(data, **options)
                    self.assertIn(diagnostic, str(default.exception))
                    with self.assertRaises(ConversionError) as strict:
                        convert_bytes(kind(data), fail_on_trace_conflicts=True, **options)
                    self.assertEqual(str(strict.exception), str(default.exception))

    def test_type_budgets_formats_and_local_validation_remain_authoritative(self):
        payload = source_for(transaction("1"), {**transaction("1"), "release": "changed"})
        local_cycle = transaction("1", "1")
        cases = [(None, {}, "input data must be bytes"),
                 (bytearray(payload), {}, "input data must be bytes"),
                 (None, {"max_input_bytes": 0}, "--max-input-bytes: byte budget"),
                 (None, {"max_decompressed_bytes": 0}, "--max-decompressed-bytes: byte budget"),
                 (b"", {}, "envelope header"),
                 (json.dumps(local_cycle).encode(), {"input_format": "json"}, "span cannot parent itself")]
        for data, options, source in self.inputs(payload):
            cases.extend([(data, {**options, "max_input_bytes": len(source)-1}, "input exceeds"),
                          (data, {**options, "input_compression": "invalid"}, "unsupported input compression"),
                          (data, {**options, "input_format": "invalid"}, "unsupported input format")])
        for data, options, diagnostic in cases:
            with self.subTest(kind=type(data), diagnostic=diagnostic, options=options):
                with self.assertRaises(ConversionError) as default:
                    convert_bytes(data, **options)
                self.assertIn(diagnostic, str(default.exception))
                with self.assertRaises(ConversionError) as strict:
                    convert_bytes(data, fail_on_trace_conflicts=True, **options)
                self.assertEqual(str(strict.exception), str(default.exception))


if __name__ == "__main__":
    unittest.main()
