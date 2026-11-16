"""Strict JSON and byte-oriented Sentry envelope framing. No SDK dependency."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json


class ConversionError(ValueError):
    """Invalid or explicitly unsupported input; no output should be published."""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConversionError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _constant(value):
    raise ConversionError(f"nonfinite JSON number: {value}")


def _decimal(text):
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ConversionError("JSON decimal exponent is outside the supported range") from exc


def decode_json(data: bytes, where: str):
    try:
        value = json.loads(data.decode("utf-8"), parse_float=_decimal,
                           parse_constant=_constant, object_pairs_hook=_pairs)
        # Reject escaped unpaired surrogates, which cannot be encoded as UTF-8.
        _validate_strings(value)
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ConversionError(f"{where}: invalid JSON/UTF-8: {exc}") from exc


def _validate_strings(value):
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, child in value.items():
            key.encode("utf-8")
            _validate_strings(child)
    elif isinstance(value, list):
        for child in value:
            _validate_strings(child)


def object_value(value, where):
    if not isinstance(value, dict):
        raise ConversionError(f"{where}: expected a JSON object")
    return value


@dataclass(frozen=True)
class Item:
    index: int
    type: str
    headers: dict
    payload: bytes
    offset: int

    def metadata(self):
        return {"index": self.index, "type": self.type, "payloadBytes": len(self.payload),
                "payloadOffset": self.offset,
                "payloadSha256": hashlib.sha256(self.payload).hexdigest()}


@dataclass(frozen=True)
class ParsedInput:
    format: str
    headers: dict
    items: list[Item]


def _line(data, pos):
    end = data.find(b"\n", pos)
    return (data[pos:], len(data)) if end < 0 else (data[pos:end], end + 1)


def _header(data, where):
    if not data or data.strip() != data:
        raise ConversionError(f"{where}: empty or padded envelope header")
    return object_value(decode_json(data, where), where)


def parse_envelope(data: bytes) -> ParsedInput:
    header_bytes, pos = _line(data, 0)
    headers = _header(header_bytes, "envelope header")
    items = []
    while pos < len(data):
        raw_header, pos = _line(data, pos)
        index = len(items)
        if pos == len(data) and not data.endswith(b"\n"):
            raise ConversionError(f"item {index}: missing newline before payload")
        item_header = _header(raw_header, f"item {index} header")
        item_type = item_header.get("type")
        if not isinstance(item_type, str) or not item_type:
            raise ConversionError(f"item {index}: missing/non-string type")
        offset = pos
        if "length" in item_header:
            length = item_header["length"]
            if type(length) is not int or length < 0:
                raise ConversionError(f"item {index}: length must be a nonnegative integer")
            end = pos + length
            if end > len(data):
                raise ConversionError(f"item {index}: truncated payload (expected {length} bytes)")
            payload = data[pos:end]
            pos = end
            if pos < len(data):
                if data[pos:pos + 1] != b"\n":
                    raise ConversionError(f"item {index}: expected newline after exact byte length")
                pos += 1
        else:
            if pos >= len(data):
                raise ConversionError(f"item {index}: missing newline-delimited payload")
            payload, pos = _line(data, pos)
        items.append(Item(index, item_type, item_header, payload, offset))
    return ParsedInput("envelope", headers, items)


def parse_input(data: bytes, input_format="auto") -> ParsedInput:
    if input_format not in {"auto", "json", "envelope"}:
        raise ConversionError(f"unsupported input format: {input_format}")
    if input_format == "envelope":
        return parse_envelope(data)
    if input_format == "json":
        value = object_value(decode_json(data, "event JSON"), "event JSON")
    else:
        try:
            value = object_value(decode_json(data, "event JSON"), "event JSON")
        except ConversionError:
            return parse_envelope(data)
    item_type = value.get("type", "event")
    if not isinstance(item_type, str) or item_type not in {"event", "transaction"}:
        raise ConversionError(f"standalone JSON: unsupported event type {item_type!r}")
    return ParsedInput("json", {}, [Item(0, item_type, {}, data, 0)])
