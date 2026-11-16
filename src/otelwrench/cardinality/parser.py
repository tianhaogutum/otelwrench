# SPDX-License-Identifier: Apache-2.0
"""Strict, original parser for the documented Prometheus text 0.0.4 subset."""

from dataclasses import dataclass
import math
import re

METRIC = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
LABEL = r"[a-zA-Z_][a-zA-Z0-9_]*"
NUMBER = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
VALUE = re.compile(rf"(?:{NUMBER}|NaN|[+-]?Inf)\Z")
NAME = re.compile(METRIC)
LABEL_NAME = re.compile(LABEL)


class InputError(ValueError):
    """Invalid or unsupported input; no partial analysis is returned."""

    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.details = details


def canonical_key(metric, labels):
    """Empty label values are equivalent to absent labels in Prometheus."""
    return metric, tuple(sorted((k, v) for k, v in labels if v != ""))


@dataclass(frozen=True)
class Sample:
    metric: str
    labels: tuple[tuple[str, str], ...]
    value: str
    timestamp_ms: str | None
    line: int
    family: str

    @property
    def key(self):
        return canonical_key(self.metric, self.labels)


@dataclass(frozen=True)
class Scrape:
    samples: tuple[Sample, ...]
    types: dict[str, str]
    help: dict[str, str]


def _decode_escapes(text, allowed, line):
    result = []
    pos = 0
    while pos < len(text):
        char = text[pos]
        if char == "\\":
            pos += 1
            if pos == len(text) or text[pos] not in allowed:
                raise InputError(f"line {line}: unsupported or incomplete escape")
            char = allowed[text[pos]]
        result.append(char)
        pos += 1
    return "".join(result)


def _labels(text, pos, line):
    labels = {}
    pos += 1  # opening brace
    while True:
        while pos < len(text) and text[pos] in " \t":
            pos += 1
        if pos < len(text) and text[pos] == "}":
            return tuple(sorted(labels.items())), pos + 1
        match = LABEL_NAME.match(text, pos)
        if not match:
            raise InputError(f"line {line}: expected an ASCII label name or closing brace")
        name = match.group()
        if name.startswith("__"):
            raise InputError(
                f"line {line}: reserved/internal label {name!r} is unsupported",
                details={"code": "reserved_input_label", "line": line, "label": name})
        if name in labels:
            raise InputError(
                f"line {line}: duplicate label {name!r}",
                details={"code": "duplicate_input_label", "line": line, "label": name})
        pos = match.end()
        while pos < len(text) and text[pos] in " \t":
            pos += 1
        if pos >= len(text) or text[pos] != "=":
            raise InputError(f"line {line}: expected '=' after label name")
        pos += 1
        while pos < len(text) and text[pos] in " \t":
            pos += 1
        if pos >= len(text) or text[pos] != '"':
            raise InputError(f"line {line}: expected quoted label value")
        pos += 1
        start = pos
        while pos < len(text) and text[pos] != '"':
            if text[pos] == "\\":
                pos += 1
            pos += 1
        if pos >= len(text):
            raise InputError(f"line {line}: unterminated label value")
        labels[name] = _decode_escapes(text[start:pos], {"\\": "\\", '"': '"', "n": "\n"}, line)
        pos += 1
        while pos < len(text) and text[pos] in " \t":
            pos += 1
        if pos < len(text) and text[pos] == "}":
            return tuple(sorted(labels.items())), pos + 1
        if pos >= len(text) or text[pos] != ",":
            raise InputError(f"line {line}: expected ',' or '}}' after label value")
        pos += 1


def _sample(text, line):
    match = NAME.match(text)
    if not match:
        raise InputError(f"line {line}: expected legacy ASCII metric name")
    metric = match.group()
    pos = match.end()
    labels = ()
    if pos < len(text) and text[pos] == "{":
        labels, pos = _labels(text, pos, line)
    if pos == len(text) or text[pos] not in " \t":
        raise InputError(f"line {line}: sample value must be separated by whitespace")
    tokens = re.split(r"[ \t]+", text[pos:].strip(" \t"))
    if not 1 <= len(tokens) <= 2 or not VALUE.fullmatch(tokens[0]):
        raise InputError(
            f"line {line}: invalid value or unsupported trailing data (e.g. exemplar)",
            details={"code": "invalid_input_sample_value", "line": line})
    value = tokens[0]
    if value not in {"NaN", "Inf", "+Inf", "-Inf"} and not math.isfinite(float(value)):
        raise InputError(
            f"line {line}: decimal value overflows float64; use explicit Inf",
            details={"code": "input_sample_value_overflow", "line": line})
    timestamp = tokens[1] if len(tokens) == 2 else None
    if timestamp is not None:
        if not re.fullmatch(r"[+-]?[0-9]+", timestamp):
            raise InputError(
                f"line {line}: timestamp must be an integer in milliseconds",
                details={"code": "invalid_input_sample_timestamp", "line": line,
                         "reason": "invalid_integer"})
        # Compare strings first to avoid unbounded integer conversion on hostile input.
        digits = timestamp.lstrip("+-").lstrip("0") or "0"
        limit = "9223372036854775808" if timestamp.startswith("-") else "9223372036854775807"
        if len(digits) > 19 or (len(digits) == 19 and digits > limit):
            raise InputError(
                f"line {line}: timestamp outside signed int64 range",
                details={"code": "invalid_input_sample_timestamp", "line": line,
                         "reason": "out_of_range"})
    return Sample(metric, labels, value, timestamp, line, metric)


def parse(text: str) -> Scrape:
    """Read exactly one scrape. Duplicate semantic series always fail."""
    if not text.endswith("\n"):
        raise InputError("Prometheus text input must end with a newline")
    if "\r" in text or "\x00" in text:
        raise InputError("CR/CRLF and NUL are unsupported; use UTF-8 with LF line endings")
    samples = []
    types, helps, metadata_lines = {}, {}, []
    for line, raw in enumerate(text.split("\n"), 1):
        if not raw.strip(" \t"):
            continue
        if raw.startswith("#"):
            directive = re.match(r"#[ \t]*(HELP|TYPE)(?:[ \t]+|$)", raw)
            if directive:
                kind = directive[1]
                rest = raw[directive.end():]
                fields = re.fullmatch(rf"({METRIC})[ \t]+(.*)", rest)
                if not fields:
                    raise InputError(f"line {line}: malformed {kind} metadata")
                name, value = fields.groups()
                target = types if kind == "TYPE" else helps
                if name in target:
                    raise InputError(f"line {line}: duplicate {kind} for {name!r}")
                if kind == "TYPE":
                    if value not in {"counter", "gauge", "histogram", "summary", "untyped"}:
                        raise InputError(f"line {line}: unsupported metric type {value!r}")
                else:
                    value = _decode_escapes(value, {"\\": "\\", "n": "\n"}, line)
                target[name] = value
                metadata_lines.append((line, kind, name))
            elif re.match(r"#[ \t]*(?:UNIT|EOF)(?:[ \t]|$)", raw):
                raise InputError(f"line {line}: OpenMetrics UNIT/EOF directives are unsupported")
            # Other comment lines carry no sample data in text 0.0.4.
            continue
        if raw[0] in " \t":
            raise InputError(f"line {line}: leading whitespace before samples is unsupported")
        samples.append(_sample(raw.rstrip(" \t"), line))
    if not samples:
        raise InputError("input contains no samples")

    owners = {}
    for family, kind in types.items():
        names = [family]
        if kind == "histogram":
            names += [family + suffix for suffix in ("_bucket", "_sum", "_count")]
        elif kind == "summary":
            names += [family + suffix for suffix in ("_sum", "_count")]
        for name in names:
            if name in owners:
                raise InputError(f"ambiguous family ownership for metric {name!r}")
            owners[name] = family

    seen, resolved, first_lines = {}, [], {}
    for sample in samples:
        family = owners.get(sample.metric, sample.metric)
        kind = types.get(family, "untyped")
        labels = dict(sample.labels)
        if kind == "histogram":
            if sample.metric == family:
                raise InputError(f"line {sample.line}: histogram must use _bucket/_sum/_count")
            bucket = sample.metric == family + "_bucket"
            if bucket != ("le" in labels):
                raise InputError(f"line {sample.line}: only histogram buckets must have an le label")
            if bucket and (not VALUE.fullmatch(labels["le"]) or labels["le"] == "NaN"):
                raise InputError(f"line {sample.line}: invalid histogram le bound")
        if kind == "summary":
            quantile = sample.metric == family
            if quantile != ("quantile" in labels):
                raise InputError(f"line {sample.line}: summary quantiles require a quantile label")
            if quantile and (not re.fullmatch(NUMBER, labels["quantile"]) or not 0 <= float(labels["quantile"]) <= 1):
                raise InputError(f"line {sample.line}: quantile must be between 0 and 1")
        if sample.key in seen:
            raise InputError(
                f"line {sample.line}: duplicate input series; first seen on line {seen[sample.key]} (timestamps do not change series identity)",
                details={"code": "duplicate_input_series", "line": sample.line,
                         "first_seen_line": seen[sample.key],
                         "original_key": {"metric": sample.key[0], "labels": dict(sample.key[1])}})
        seen[sample.key] = sample.line
        first_lines.setdefault(family, sample.line)
        resolved.append(Sample(sample.metric, sample.labels, sample.value, sample.timestamp_ms, sample.line, family))
    for line, kind, family in metadata_lines:
        if family in first_lines and line > first_lines[family]:
            raise InputError(f"line {line}: {kind} must precede samples for {family!r}")
    return Scrape(tuple(resolved), types, helps)
