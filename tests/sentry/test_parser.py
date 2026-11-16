import hashlib
import json
import unittest

from otelwrench.sentry.parser import ConversionError, decode_json, parse_envelope, parse_input


def envelope(payload, *, kind="event", length=True, trailer=True):
    header = {"type": kind}
    if length:
        header["length"] = len(payload)
    return b"{}\n" + json.dumps(header).encode() + b"\n" + payload + (b"\n" if trailer else b"")


class ParserTests(unittest.TestCase):
    def test_length_is_utf8_bytes_and_includes_embedded_newlines(self):
        payload = '{\n  "message": "错误 café",\n  "timestamp": 1700000000.123456789\n}'.encode()
        source = envelope(payload)
        result = parse_envelope(source)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].payload, payload)
        metadata = result.items[0].metadata()
        self.assertEqual(metadata["payloadBytes"], len(payload))
        self.assertEqual(metadata["payloadSha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(source[metadata["payloadOffset"]:metadata["payloadOffset"] + len(payload)], payload)

    def test_character_count_is_not_byte_count(self):
        payload = '{"message":"错误"}'.encode()
        source = b'{}\n{"type":"event","length":16}\n' + payload
        with self.assertRaisesRegex(ConversionError, "expected newline"):
            parse_envelope(source)

    def test_mixed_length_delimited_and_line_delimited(self):
        first, second = b'{\n"message":"one"\n}', b'{"message":"two"}'
        source = envelope(first) + b'{"type":"event"}\n' + second
        parsed = parse_envelope(source)
        self.assertEqual([i.payload for i in parsed.items], [first, second])

    def test_optional_final_newline_for_both_framings(self):
        for length in (True, False):
            for trailer in (True, False):
                with self.subTest(length=length, trailer=trailer):
                    self.assertEqual(parse_envelope(envelope(b'{}', length=length, trailer=trailer)).items[0].payload, b'{}')

    def test_arbitrary_binary_payload_framing(self):
        payload = b'\x00\n\xff\n{}\n'
        parsed = parse_envelope(envelope(payload, kind="attachment"))
        self.assertEqual(parsed.items[0].payload, payload)

    def test_empty_envelopes(self):
        for source in (b'{}', b'{}\n', b'{"event_id":"' + b'a' * 32 + b'"}'):
            self.assertEqual(parse_envelope(source).items, [])

    def test_explicit_zero_length_payload(self):
        parsed = parse_envelope(envelope(b"", kind="attachment"))
        self.assertEqual(parsed.items[0].payload, b"")

    def test_missing_newline_before_zero_length_payload_fails(self):
        with self.assertRaisesRegex(ConversionError, "missing newline before payload"):
            parse_envelope(b'{}\n{"type":"attachment","length":0}')

    def test_malformed_framing(self):
        bad = [b"", b' {}\n', b'{} \n', b'{}\r\n', b'[]\n', b'{}\n\n',
               b'{}\n{"type":"event"}', b'{}\n{"type":"event"}\n',
               b'{}\n{"type":"event","length":8}\n{}',
               b'{}\n{"type":"event","length":1}\n{}',
               envelope(b'{}') + b'\n', envelope(b'{}') + b' ',
               b'{}\n{}\n{}', b'{}\n{"type":null}\n{}', b'{}\n{"type":""}\n{}']
        for source in bad:
            with self.subTest(source=source), self.assertRaises(ConversionError):
                parse_envelope(source)

    def test_invalid_lengths(self):
        for length in (-1, 1.5, True, None, "2", [], {}):
            source = b'{}\n' + json.dumps({"type": "event", "length": length}).encode() + b'\n{}'
            with self.subTest(length=length), self.assertRaisesRegex(ConversionError, "length must"):
                parse_envelope(source)

    def test_invalid_json_and_encoding(self):
        for source in (b'{', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}',
                       b'{"x":1,"x":2}', b'{"x":"\xff"}', b'{"x":"\\ud800"}',
                       b'{"\\udfff":1}', b'\xef\xbb\xbf{}', b'{"x":1} trailing'):
            with self.subTest(source=source), self.assertRaises(ConversionError):
                decode_json(source, "test")

    def test_precision_preserved_during_json_decode(self):
        result = decode_json(b'{"timestamp":1700000000.123456789}', "test")
        self.assertEqual(str(result["timestamp"]), "1700000000.123456789")

    def test_auto_and_explicit_modes(self):
        self.assertEqual(parse_input(b'{"message":"hello"}').format, "json")
        self.assertEqual(parse_input(envelope(b'{"message":"hello"}')).format, "envelope")
        self.assertEqual(parse_input(b'{}', "envelope").items, [])
        with self.assertRaises(ConversionError):
            parse_input(envelope(b'{}'), "json")
        with self.assertRaises(ConversionError):
            parse_input(b'{}', "xml")

    def test_unsupported_standalone_type(self):
        with self.assertRaisesRegex(ConversionError, "unsupported event type"):
            parse_input(b'{"type":"replay_event"}')


if __name__ == "__main__":
    unittest.main()
