"""Strict configuration and exact JSON growth budgets, shared by CLI/library."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
import json
import math
from pathlib import Path
import sys

from ._files import MAX_INPUT_BYTES, read_text
from .parser import InputError


DEFAULT_POLICY = {
    "family_added": "warn",
    "family_removed": "fail",
    "type_changed": "fail",
    "help_changed": "warn",
    "label_schema_changed": "fail",
    "series_added": "warn",
    "series_missing": "warn",
    "series_growth": "fail",
}
MAX_GROWTH_PERCENT = 1_000_000


@dataclass(frozen=True)
class _JsonGrowthLimit:
    """JSON token whose exact decimal differs from the numeric display.

    A positive float-underflow token needs no enormous rational expansion:
    every positive series-growth ratio is at least 100 / sys.maxsize (len's
    upper bound), so it exceeds such a limit. None marks that case, not zero.
    """
    display: float
    token: str
    ratio: Fraction | None

    @classmethod
    def from_token(cls, token: str, display: float) -> float | _JsonGrowthLimit:
        if display == 0:
            mantissa = token.lower().split("e", 1)[0]
            return cls(display, token, None) if any(c in "123456789" for c in mantissa) else display
        # Non-underflow, in-range tokens have representable Decimal exponents.
        # String construction and integer-ratio conversion ignore caller context;
        # Decimal also avoids Python's int-from-string digit limit for long tokens.
        ratio = Fraction(Decimal(token))
        return display if ratio == Fraction(str(display)) else cls(display, token, ratio)


@dataclass
class Config:
    policy: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_POLICY))
    max_series_growth_percent: int | float | _JsonGrowthLimit = 20

    @classmethod
    def from_json(cls, text: str, *, source: str = "config") -> Config:
        """Load strict JSON text with exact numeric growth-limit semantics.

        Raise InputError for non-string text, JSON decoding or config errors.
        source labels decoding diagnostics; no file I/O or size limit is applied.
        """
        return cls.from_dict(_decode_json(text, source=source, growth_config=True))

    @classmethod
    def from_file(cls, path: str | Path, *, max_bytes: int = MAX_INPUT_BYTES) -> Config:
        """Load an independent config from a bounded strict UTF-8 JSON file.

        Use the same arguments/read rules as with_overrides_file, then from_json
        with path as source. Validate limit, path, read/UTF-8, JSON, then schema.
        Raise InputError without writes or changes to caller Decimal context;
        read/decoding errors name path, schema errors retain field names.
        """
        return cls.from_json(_read_config_file(path, max_bytes=max_bytes), source=str(path))

    def with_overrides(self, data: object) -> Config:
        """Return an independent validated copy with partial policy overlays.

        Validate both base state and data as in from_dict. Omitted fields keep
        current effective values, including exact JSON budgets and evidence;
        a supplied limit replaces the budget using from_dict numeric semantics.
        Raise InputError without mutating either input if validation fails.
        """
        config = Config.from_dict({"policy": self.policy,
                                   "max_series_growth_percent": self.max_series_growth_percent})
        overrides = Config.from_dict(data)
        for kind in data.get("policy", {}):
            config.policy[kind] = overrides.policy[kind]
        if "max_series_growth_percent" in data:
            config.max_series_growth_percent = overrides.max_series_growth_percent
        return config

    def with_overrides_json(self, text: str, *, source: str = "config overrides") -> Config:
        """Return an independent overlay with exact JSON replacement budgets.

        Decode strict JSON first, then validate base/overrides as with_overrides;
        invalid base state cannot be repaired. Raise InputError without mutation.
        source labels decoding diagnostics. Callers must bound text size; this
        method performs no file I/O or size limiting.
        """
        return self.with_overrides(_decode_json(text, source=source, growth_config=True))

    def with_overrides_file(self, path: str | Path, *, max_bytes: int = MAX_INPUT_BYTES) -> Config:
        """Read a bounded strict UTF-8 JSON file, then apply exact text overrides.

        Accept str/Path and a plain int max_bytes in 0..sys.maxsize-1 (64 MiB
        by default). Read at most max_bytes+1 bytes, without file writes. Invalid
        arguments, read/decode and overlay errors raise InputError; read/decode
        diagnostics name path. Schema/base validation follows with_overrides_json.
        Neither base state nor caller Decimal context is changed.
        """
        return self.with_overrides_json(_read_config_file(path, max_bytes=max_bytes), source=str(path))

    def to_json(self) -> str:
        """Export compact, sorted JSON with full policy and exact numeric budget.

        Validate/merge defaults as in from_dict; raise InputError for invalid
        state without mutating this instance or the caller's Decimal context.
        """
        config = Config.from_dict({"policy": self.policy,
                                   "max_series_growth_percent": self.max_series_growth_percent})
        growth = config.max_series_growth_percent
        # The retained token came from strict JSON decoding. Emit it directly as
        # a number: a float conversion loses intent; a string changes the schema.
        limit = growth.token if type(growth) is _JsonGrowthLimit else json.dumps(growth, allow_nan=False)
        policy = json.dumps(config.policy, sort_keys=True, separators=(",", ":"))
        return '{"max_series_growth_percent":' + limit + ',"policy":' + policy + '}'

    @classmethod
    def from_dict(cls, data: object) -> Config:
        if not isinstance(data, dict) or set(data) - {"policy", "max_series_growth_percent"}:
            raise InputError("config must be an object with only policy/max_series_growth_percent")
        config = cls()
        policy = data.get("policy", {})
        if not isinstance(policy, dict):
            raise InputError("config.policy must be an object")
        for kind, severity in policy.items():
            if kind not in DEFAULT_POLICY or severity not in ("warn", "fail"):
                raise InputError(f"invalid policy entry {kind!r}: {severity!r}; expected known class and warn/fail")
            config.policy[kind] = severity
        growth = data.get("max_series_growth_percent", 20)
        display = growth.display if type(growth) is _JsonGrowthLimit else growth
        if type(display) not in (int, float) or not 0 <= display <= MAX_GROWTH_PERCENT:
            raise InputError(f"max_series_growth_percent must be finite and within 0..{MAX_GROWTH_PERCENT}")
        config.max_series_growth_percent = growth
        return config


def _read_config_file(path: str | Path, *, max_bytes: int) -> str:
    """Shared public config file argument rules around the CLI's bounded reader."""
    if type(max_bytes) is not int or not 0 <= max_bytes < sys.maxsize:
        raise InputError(f"{path}: max_bytes must be an integer within 0..{sys.maxsize - 1}")
    if not isinstance(path, (str, Path)) or "\0" in str(path):
        raise InputError(f"{path!r}: path must be a str or Path without NUL characters")
    try:
        return read_text(path, max_bytes=max_bytes)
    except InputError:
        raise
    except (ValueError, OverflowError) as exc:
        # Label platform path/read-size failures as well as ordinary I/O.
        raise InputError(f"{path}: {exc}") from exc


def _decode_json(text: str, *, source: str, growth_config: bool = False) -> object:
    """Shared strict decoder for config text and CLI JSON files."""
    if not isinstance(text, str):
        raise InputError(f"{source}: JSON text must be a string")

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InputError(f"{source}: duplicate JSON key {key!r}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise InputError(f"{source}: non-finite JSON constant {value}")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise InputError(f"{source}: JSON number overflows finite float range")
        if growth_config:
            # The growth limit is the config's only permitted fractional field.
            # Reject negative nonzero mantissas even if they underflow to -0.0;
            # signed zero and arbitrarily tiny positive numbers remain valid.
            mantissa = value.lower().split("e", 1)[0]
            negative = value.startswith("-") and any(c in "123456789" for c in mantissa)
            # Only a float rounded onto the upper endpoint needs an exact
            # comparison. Its nonzero magnitude ensures a representable Decimal
            # exponent; string construction/comparison never rounds or sets flags.
            above = number > MAX_GROWTH_PERCENT or (
                number == MAX_GROWTH_PERCENT and Decimal(value) > MAX_GROWTH_PERCENT)
            if negative or above:
                raise InputError(f"{source}: max_series_growth_percent must be finite and within 0..{MAX_GROWTH_PERCENT}")
            return _JsonGrowthLimit.from_token(value, number)
        return number

    try:
        return json.loads(text, object_pairs_hook=unique_keys,
                          parse_constant=invalid_constant, parse_float=finite_float)
    except (ValueError, RecursionError) as exc:
        raise InputError(f"{source}: invalid JSON: {exc}") from exc

