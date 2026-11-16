"""Direct library inputs use their actual bytes, never subclass behavior."""
import gzip
import hashlib
import sys
import unittest
from unittest.mock import patch

from otelwrench.sentry.mapping import convert_bytes
from otelwrench.sentry.parser import ConversionError
from .test_parser import envelope

EVENT = b'{"message":"synthetic"}'


class UnderreportedBytes(bytes):
    def __len__(self):
        return 1


class OverreportedBytes(bytes):
    def __len__(self):
        return sys.maxsize


class FalsyBytes(bytes):
    def __bool__(self):
        return False


class TruthyBytes(bytes):
    def __bool__(self):
        return True


class NoHooksBytes(bytes):
    def forbidden(self, *args, **kwargs):
        raise AssertionError("convert_bytes must use actual byte content")

    __len__ = __bool__ = __bytes__ = __iter__ = __getitem__ = forbidden
    decode = find = strip = endswith = forbidden


SUBCLASSES = (UnderreportedBytes, OverreportedBytes, FalsyBytes, TruthyBytes, NoHooksBytes)


class DirectBytesTests(unittest.TestCase):
    def test_actual_overflow_precedes_normalization_gzip_parser_and_mapping(self):
        for compression in ("none", "gzip"):
            # Malformed data must still fail on input size first.
            for payload in (EVENT, b"invalid JSON or gzip"):
                source = gzip.compress(payload, mtime=0) if compression == "gzip" else payload
                for subclass in SUBCLASSES:
                    for budget in (1, len(source) - 1):
                        with self.subTest(compression=compression, subclass=subclass,
                                          payload=payload, budget=budget), \
                                patch("otelwrench.sentry.compression.gzip.GzipFile") as decode, \
                                patch("otelwrench.sentry.mapping.parse_input") as parse, \
                                patch("otelwrench.sentry.mapping.convert_error") as mapping:
                            with self.assertRaisesRegex(ConversionError, f"input exceeds --max-input-bytes={budget}$"):
                                convert_bytes(subclass(source), input_compression=compression,
                                              max_input_bytes=budget)
                            decode.assert_not_called()
                            parse.assert_not_called()
                            mapping.assert_not_called()

    def test_exact_budgets_preserve_documents_framing_and_provenance(self):
        multiline = '{\n"message":"错误 café"\n}'.encode()
        mixed = envelope(multiline) + envelope(EVENT, length=False, trailer=False)[3:]
        # All auto/explicit framing paths, including empty envelopes, are retained.
        for payload, formats, expected_items in (
                (EVENT, ("auto", "json"), 1),
                (mixed, ("auto", "envelope"), 2),
                (b"{}\n", ("envelope",), 0)):
            for compression in ("none", "gzip"):
                source = gzip.compress(payload, mtime=0) if compression == "gzip" else payload
                for input_format in formats:
                    options = dict(input_format=input_format, input_compression=compression,
                                   max_input_bytes=len(source), max_decompressed_bytes=len(payload))
                    expected = convert_bytes(source, **options)
                    for subclass in SUBCLASSES:
                        with self.subTest(subclass=subclass, compression=compression,
                                          input_format=input_format, payload=payload):
                            actual = convert_bytes(subclass(source), **options)
                            self.assertEqual(actual, expected)
                            manifest = actual["manifest.json"]
                            self.assertEqual(manifest["input"]["bytes"], len(source))
                            self.assertEqual(manifest["input"]["sha256"], hashlib.sha256(source).hexdigest())
                            self.assertEqual(manifest["summary"]["inputItems"], expected_items)
                            self.assertEqual(manifest["summary"]["logRecords"], expected_items)
                            if compression == "gzip":
                                self.assertEqual(manifest["input"]["decompressedBytes"], len(payload))
                                self.assertEqual(manifest["input"]["decompressedSha256"], hashlib.sha256(payload).hexdigest())
                            for item in manifest["items"]:
                                offset, count = item["payloadOffset"], item["payloadBytes"]
                                framed = payload[offset:offset + count]
                                self.assertIn(framed, (EVENT, multiline))
                                self.assertEqual(item["payloadSha256"], hashlib.sha256(framed).hexdigest())

    def test_empty_subclasses_follow_ordinary_empty_input_errors(self):
        for compression in ("none", "gzip"):
            for input_format in ("auto", "json", "envelope"):
                options = dict(input_compression=compression, input_format=input_format)
                with self.assertRaises(ConversionError) as ordinary:
                    convert_bytes(b"", **options)
                for subclass in SUBCLASSES:
                    with self.subTest(compression=compression, input_format=input_format,
                                      subclass=subclass), \
                            patch("otelwrench.sentry.compression.gzip.GzipFile") as decode:
                        with self.assertRaises(ConversionError) as actual:
                            convert_bytes(subclass(b""), **options)
                        self.assertEqual(str(actual.exception), str(ordinary.exception))
                        decode.assert_not_called()

    def test_budget_validation_and_compression_format_precedence(self):
        for name in ("max_input_bytes", "max_decompressed_bytes"):
            for invalid in (0, True, sys.maxsize):
                with self.subTest(name=name, invalid=invalid), \
                        patch("otelwrench.sentry.compression.gzip.GzipFile") as decode, \
                        patch("otelwrench.sentry.mapping.parse_input") as parse:
                    with self.assertRaisesRegex(ConversionError, "--" + name.replace("_", "-") + ": byte budget"):
                        convert_bytes(NoHooksBytes(EVENT), **{name: invalid},
                                      input_compression="invalid", input_format="invalid")
                    decode.assert_not_called()
                    parse.assert_not_called()
        for options, message in (
                (dict(max_input_bytes=1, input_compression="invalid", input_format="invalid"), "input exceeds --max-input-bytes"),
                (dict(input_compression="invalid", input_format="invalid"), "unsupported input compression"),
                (dict(input_format="invalid"), "unsupported input format")):
            with self.subTest(options=options), self.assertRaisesRegex(ConversionError, message):
                convert_bytes(NoHooksBytes(EVENT), **options)
        with self.assertRaisesRegex(ConversionError, "--max-input-bytes: byte budget"):
            convert_bytes(NoHooksBytes(EVENT), max_input_bytes=0, max_decompressed_bytes=0)

    def test_subclasses_cannot_hide_invalid_suffix_or_later_items(self):
        for payload, input_format in ((EVENT + b"invalid suffix", "json"),
                                      (envelope(EVENT) + b"invalid header\n{}", "envelope")):
            for compression in ("none", "gzip"):
                source = gzip.compress(payload, mtime=0) if compression == "gzip" else payload
                options = dict(input_compression=compression, input_format=input_format)
                with self.assertRaises(ConversionError) as ordinary:
                    convert_bytes(source, **options)
                for subclass in SUBCLASSES:
                    with self.subTest(compression=compression, subclass=subclass, input_format=input_format):
                        with self.assertRaises(ConversionError) as actual:
                            convert_bytes(subclass(source), **options)
                        self.assertEqual(str(actual.exception), str(ordinary.exception))

    def test_gzip_expansion_limit_is_independent_of_source_subclass(self):
        source = gzip.compress(EVENT, mtime=0)
        for subclass in SUBCLASSES:
            with self.subTest(subclass=subclass), self.assertRaisesRegex(
                    ConversionError, "gzip expansion exceeds --max-decompressed-bytes"):
                convert_bytes(subclass(source), input_compression="gzip",
                              max_input_bytes=len(source), max_decompressed_bytes=len(EVENT) - 1)

    def test_unsupported_direct_input_is_not_coerced(self):
        class Convertible:
            def forbidden(self, *args):
                raise AssertionError("unsupported input must not invoke conversion hooks")
            __bytes__ = __len__ = __bool__ = __iter__ = forbidden

        for source in (None, "", EVENT.decode(), bytearray(EVENT), memoryview(EVENT), Convertible()):
            with self.subTest(input_type=type(source)):
                with self.assertRaisesRegex(ConversionError, "input data must be bytes"):
                    convert_bytes(source)
                with self.assertRaisesRegex(ConversionError, "--max-decompressed-bytes: byte budget"):
                    convert_bytes(source, max_decompressed_bytes=0)


if __name__ == "__main__":
    unittest.main()
