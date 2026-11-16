"""Strict, deliberately bounded Prometheus 0.0.4 reader.

The upstream parser is deliberately permissive. Validate the supported grammar
before using it, and retain wire names (including counter ``_total`` suffixes).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Context, Decimal, InvalidOperation
import math
import re

from prometheus_client.parser import text_string_to_metric_families


class InputError(ValueError):
    """Malformed or unsupported input; suitable for a CLI diagnostic."""


NAME = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
LABEL = r"[a-zA-Z_][a-zA-Z0-9_]*"
NUMBER = r"(?:[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|NaN|[+-]?Inf)"
PAIR = rf'{LABEL}[ \t]*=[ \t]*"(?:[^"\\\n\r]|\\[\\n"])*"'
SAMPLE = re.compile(
    rf'(?P<name>{NAME})(?:[ \t]*\{{(?P<labels>[ \t]*(?:{PAIR}(?:[ \t]*,[ \t]*{PAIR})*[ \t]*,?)?[ \t]*)\}})?'
    rf'[ \t]+(?P<value>{NUMBER})(?:[ \t]+(?P<timestamp>[+-]?[0-9]+))?[ \t]*'
)
TYPES = {"counter", "gauge", "histogram", "summary", "untyped"}
Series = tuple[str, tuple[tuple[str, str], ...]]


@dataclass
class Family:
    name: str
    type: str
    help: str | None
    unit: str | None = None  # 0.0.4 has no UNIT metadata; do not infer from names.
    schemas: dict[str, tuple[str, ...]] = field(default_factory=dict)
    series: set[Series] = field(default_factory=set)

    def contract(self) -> dict:
        return {
            "type": self.type,
            "help": self.help,
            "unit": self.unit,
            "samples": self.schema(),
        }

    def schema(self) -> list[dict]:
        return [{"name": n, "label_keys": list(self.schemas[n])} for n in sorted(self.schemas)]


@dataclass
class Snapshot:
    families: dict[str, Family]

    @property
    def series(self) -> set[Series]:
        return {series for family in self.families.values() for series in family.series}

    def summary(self) -> dict:
        return {"families": len(self.families), "observed_series": len(self.series)}


@dataclass
class _CompositeSample:
    line: int
    name: str
    labels: tuple[tuple[str, str], ...]
    value: str


def _help_valid(value: str) -> bool:
    return re.fullmatch(r'(?:[^\\]|\\[\\n])*', value) is not None


def parse_snapshot(text: str, source: str = "snapshot", *, validate_composites: bool = False) -> Snapshot:
    """Parse a complete UTF-8/LF exposition, rejecting unsupported extensions.

    Every sample is accounted for. Metadata-only families are preserved. Classic
    histogram/summary suffixes belong to the declared parent, never guessed from
    suffixes without TYPE metadata. Opt-in numerical consistency checks use
    exact wire decimals; values are discarded before returning the snapshot.
    """
    def fail(message: str, line: int | None = None) -> None:
        where = f"{source}:{line}" if line is not None else source
        raise InputError(f"{where}: {message}")

    if "\r" in text or "\x00" in text:
        fail("only UTF-8 text with LF line endings and no NUL is supported")
    if text and not text.endswith("\n"):
        fail("nonempty exposition must end with LF")
    metadata: dict[str, dict] = {}
    records: list[tuple[int, str, str]] = []
    # First validate wire syntax. Resolve family ownership only after all TYPEs
    # are known, so late declarations cannot hide samples in untyped families.
    for lineno, original in enumerate(text.split("\n"), 1):
        line = original.strip(" \t")
        if not line:
            continue
        if line.startswith("#"):
            tokens = line[1:].strip(" \t").split(None, 2)
            if not tokens:
                continue
            directive = tokens[0]
            if directive in {"UNIT", "EOF", "CREATED"}:
                fail(f"unsupported OpenMetrics directive {directive}; use Prometheus 0.0.4", lineno)
            if directive not in {"HELP", "TYPE"}:
                continue
            if len(tokens) < 2 or not re.fullmatch(NAME, tokens[1]):
                fail(f"invalid {directive} metric name (legacy names required)", lineno)
            name = tokens[1]
            value = tokens[2] if len(tokens) == 3 else ""
            meta = metadata.setdefault(name, {})
            if directive in meta:
                fail(f"duplicate/conflicting {directive} for {name}", lineno)
            if directive == "TYPE" and value not in TYPES:
                fail(f"unsupported TYPE {value!r}", lineno)
            if directive == "HELP" and not _help_valid(value):
                fail("invalid HELP escape; only backslash and newline escapes are supported", lineno)
            meta[directive] = (value, lineno)
        else:
            match = SAMPLE.fullmatch(line)
            if not match:
                fail("malformed/unsupported sample, label escape, timestamp, or exemplar", lineno)
            timestamp = match["timestamp"]
            if timestamp is not None:
                digits = timestamp.lstrip("+-").lstrip("0") or "0"
                normalized = ("-" if timestamp.startswith("-") else "") + digits
                if len(digits) > 19 or not -(2**63) <= int(normalized) < 2**63:
                    fail("timestamp is outside signed int64 milliseconds", lineno)
            records.append((lineno, match["name"], line))

    owners: dict[str, str] = {}
    for name, meta in metadata.items():
        typ = meta.get("TYPE", ("untyped", 0))[0]
        suffixes = ["_bucket", "_sum", "_count"] if typ == "histogram" else (
            ["", "_sum", "_count"] if typ == "summary" else [""]
        )
        for suffix in suffixes:
            sample_name = name + suffix
            if sample_name in owners and owners[sample_name] != name:
                fail(f"ambiguous family ownership for {sample_name}")
            owners[sample_name] = name
    # A separate HELP/TYPE for a composite component is ambiguous even when it
    # has no samples. Also reject a bare histogram-name sample explicitly.
    for name in metadata:
        if name in owners and owners[name] != name:
            fail(f"metadata for composite component {name} conflicts with its parent")

    groups: dict[str, list[tuple[int, str, str]]] = {name: [] for name in metadata}
    sequence: list[tuple[int, str]] = []
    for name, meta in metadata.items():
        sequence.extend((lineno, name) for _, lineno in meta.values())
    for lineno, sample_name, line in records:
        owner = owners.get(sample_name, sample_name)
        if owner in metadata and metadata[owner].get("TYPE", (None,))[0] == "histogram" and sample_name == owner:
            fail("histogram must use _bucket/_sum/_count samples", lineno)
        meta = metadata.get(owner, {})
        if any(meta_line > lineno for _, meta_line in meta.values()):
            fail(f"HELP/TYPE for {owner} must precede its samples", lineno)
        groups.setdefault(owner, []).append((lineno, sample_name, line))
        sequence.append((lineno, owner))
    previous = None
    closed: set[str] = set()
    for lineno, owner in sorted(sequence):
        if owner != previous:
            if owner in closed:
                fail(f"family {owner} is not in one contiguous group", lineno)
            if previous is not None:
                closed.add(previous)
            previous = owner

    result: dict[str, Family] = {}
    all_series: set[Series] = set()
    for name, rows in groups.items():
        meta = metadata.get(name, {})
        typ = meta.get("TYPE", ("untyped",))[0]
        headers = [f"# TYPE {name} {typ}"]
        if "HELP" in meta:
            headers.append(f"# HELP {name} {meta['HELP'][0]}")
        block = "\n".join(headers + [line for _, _, line in rows]) + "\n"
        try:
            parsed = list(text_string_to_metric_families(block))
        except (ValueError, OverflowError) as exc:
            fail(f"prometheus_client could not parse family {name}: {exc}")
        if len(parsed) != 1 or len(parsed[0].samples) != len(rows):
            fail(f"unsupported family layout for {name}")
        metric = parsed[0]
        family = Family(name, typ, metric.documentation if "HELP" in meta else None)
        composite_samples = []
        for (lineno, wire_name, line), sample in zip(rows, metric.samples, strict=True):
            labels = sample.labels
            if any(not re.fullmatch(LABEL, key) or key.startswith("__") for key in labels):
                fail("unsupported/reserved label name", lineno)
            keys = tuple(sorted(labels))
            if wire_name in family.schemas and family.schemas[wire_name] != keys:
                fail(f"inconsistent label-key schema for {wire_name}", lineno)
            family.schemas[wire_name] = keys
            identity = (wire_name, tuple(sorted(labels.items())))
            if identity in all_series:
                fail(f"duplicate series for {wire_name}", lineno)
            family.series.add(identity)
            all_series.add(identity)
            if typ in {"histogram", "summary"}:
                composite_samples.append(_CompositeSample(
                    lineno, wire_name, identity[1], SAMPLE.fullmatch(line)["value"]))
        _validate_composite(family, composite_samples, fail, validate_composites)
        result[name] = family
    return Snapshot(result)


def _validate_composite(family: Family, samples: list[_CompositeSample], fail,
                        validate_values: bool) -> None:
    if not samples:
        return
    special = "le" if family.type == "histogram" else "quantile"
    special_name = family.name + "_bucket" if family.type == "histogram" else family.name
    base_keys = None
    groups: dict[tuple, list[_CompositeSample]] = {}
    for sample in samples:
        labels = dict(sample.labels)
        is_special = sample.name == special_name
        if is_special != (special in labels):
            fail(f"{family.name}: {sample.name} has invalid {special} label placement", sample.line)
        if is_special:
            if not re.fullmatch(NUMBER, labels[special]):
                fail(f"{family.name}: invalid {special} value", sample.line)
            if validate_values and special == "quantile":
                # Diagnose unsupported exponents before float overflow can
                # mask them as ordinary range errors. Keep default screening.
                _composite_number(labels[special], family, sample, fail)
            try:
                boundary = float(labels[special])
            except ValueError:
                fail(f"{family.name}: invalid {special} value", sample.line)
            if math.isnan(boundary) or (special == "quantile" and not 0 <= boundary <= 1):
                fail(f"{family.name}: invalid {special} value", sample.line)
        base = tuple((k, v) for k, v in sample.labels if k != special)
        keys = tuple(k for k, _ in base)
        if base_keys is not None and base_keys != keys:
            fail(f"{family.name}: inconsistent base label schema across components", sample.line)
        base_keys = keys
        groups.setdefault(base, []).append(sample)
    # Inspect observed groups only. Metadata alone establishes no observations;
    # summaries do not require any quantile samples (e.g. client_python output).
    for group in groups.values():
        names = {sample.name for sample in group}
        for suffix in ("_sum", "_count"):
            if family.name + suffix not in names:
                fail(f"{family.name}: missing required {family.name + suffix}; "
                     "each observed group requires matching _sum and _count", group[0].line)
        if family.type == "histogram" and not any(
                sample.name == special_name and dict(sample.labels)[special] == "+Inf" for sample in group):
            fail(f"{family.name}: each histogram group requires buckets including le=\"+Inf\"", group[0].line)
        if validate_values:
            _validate_composite_values(family, group, fail)


def _composite_number(raw: str, family: Family, sample: _CompositeSample, fail) -> Decimal:
    try:
        # String construction stays exact; the private error context makes
        # unsupported exponents independent of caller traps and flags.
        return Decimal(raw, context=Context(traps=[InvalidOperation]))
    except InvalidOperation:
        fail(f"{family.name}: numeric exponent outside supported decimal range", sample.line)


def _validate_composite_values(family: Family, group: list[_CompositeSample], fail) -> None:
    def count(sample: _CompositeSample) -> Decimal:
        value = _composite_number(sample.value, family, sample, fail)
        if not value.is_finite() or value < 0 or value != value.to_integral_value():
            fail(f"{family.name}: {sample.name} must be a finite nonnegative integer", sample.line)
        return value

    count_sample = next(sample for sample in group if sample.name == family.name + "_count")
    total = count(count_sample)
    if family.type != "histogram":
        quantile_lines: dict[Decimal, int] = {}
        for sample in group:
            if sample.name != family.name:
                continue
            quantile = _composite_number(dict(sample.labels)["quantile"], family, sample, fail)
            if not 0 <= quantile <= 1:
                fail(f"{family.name}: invalid quantile value (exact label outside [0,1])", sample.line)
            if quantile in quantile_lines:
                fail(f"{family.name}: numerically duplicate quantile "
                     f"(previous quantile at line {quantile_lines[quantile]})", sample.line)
            quantile_lines[quantile] = sample.line
        return
    previous = None
    for sample in group:
        if sample.name != family.name + "_bucket":
            continue
        boundary = _composite_number(dict(sample.labels)["le"], family, sample, fail)
        value = count(sample)
        if previous is not None:
            old_boundary, old_value, old_line = previous
            if boundary <= old_boundary:
                fail(f"{family.name}: bucket boundaries must be strictly increasing "
                     f"(previous bucket at line {old_line})", sample.line)
            if value < old_value:
                fail(f"{family.name}: cumulative bucket count decreases "
                     f"(previous bucket at line {old_line})", sample.line)
        if dict(sample.labels)["le"] == "+Inf" and value != total:
            fail(f'{family.name}: le="+Inf" bucket must equal _count at line {count_sample.line}', sample.line)
        previous = (boundary, value, sample.line)
