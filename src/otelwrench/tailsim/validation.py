"""Shared strict validation. No unknown input is silently discarded."""

import json
import math


class InputError(ValueError):
    """Invalid or unsupported corpus/configuration."""


def obj(value, path, allowed, required=()):
    if not isinstance(value, dict):
        raise InputError(f"{path}: expected an object")
    unknown = value.keys() - set(allowed)
    missing = set(required) - value.keys()
    if unknown:
        raise InputError(f"{path}: unsupported field(s): {', '.join(sorted(unknown))}")
    if missing:
        raise InputError(f"{path}: missing field(s): {', '.join(sorted(missing))}")
    return value


def array(value, path, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise InputError(f"{path}: expected {'nonempty ' if nonempty else ''}array")
    return value


def string(value, path, nonempty=False):
    if not isinstance(value, str) or (nonempty and not value):
        raise InputError(f"{path}: expected {'nonempty ' if nonempty else ''}string")
    return value


def integer(value, path, maximum=2**64 - 1, minimum=0, strings=False):
    if strings and isinstance(value, str):
        if not value or not value.isascii() or not value.isdecimal():
            if not (minimum < 0 and value.startswith('-') and value[1:].isascii() and value[1:].isdecimal()):
                raise InputError(f"{path}: expected decimal integer")
        try:
            value = int(value)
        except ValueError as exc:
            raise InputError(f"{path}: invalid integer") from exc
    if type(value) is not int or not minimum <= value <= maximum:
        raise InputError(f"{path}: expected integer in [{minimum}, {maximum}]")
    return value


def finite_number(value, path):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise InputError(f"{path}: expected finite number")


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"JSON: duplicate key {key!r}")
        result[key] = value
    return result


def parse_json(text):
    def bad_constant(value):
        raise InputError(f"JSON: nonfinite number {value} is unsupported")
    try:
        return json.loads(text, object_pairs_hook=unique_pairs, parse_constant=bad_constant)
    except (ValueError, RecursionError) as exc:
        raise InputError(f"invalid JSON: {exc}") from exc


def canonical_bytes(value):
    """Our measured byte representation: compact sorted UTF-8 JSON, no newline."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')
