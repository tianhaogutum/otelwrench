"""Bounded, strict local input readers. No network or object construction."""

import json
from pathlib import Path

import yaml


class PreviewError(ValueError):
    """Invalid or unsupported input, with a location suitable for CLI errors."""


def fail(where, message):
    raise PreviewError(f"{where}: {message}")


def obj(value, where, allowed):
    if not isinstance(value, dict):
        fail(where, "expected an object")
    unknown = value.keys() - set(allowed)
    if unknown:
        fail(where, f"unsupported fields: {', '.join(sorted(map(str, unknown)))}")
    return value


def seq(value, where):
    if not isinstance(value, list):
        fail(where, "expected an array")
    return value


def string(value, where, nonempty=False):
    if not isinstance(value, str) or (nonempty and not value):
        fail(where, "expected " + ("a nonempty string" if nonempty else "a string"))
    try:
        value.encode("utf-8")
    except UnicodeError:
        fail(where, "invalid Unicode surrogate")
    return value


def uint(value, where, bits=32):
    if type(value) is not int or not 0 <= value < 2**bits:
        fail(where, f"expected uint{bits} integer")
    return value


def read_text(path, limit):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        fail(str(path), f"input exceeds {limit} byte limit")
    return data.decode("utf-8")


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("JSON", f"duplicate object key {key!r}")
        result[key] = value
    return result


def read_json(path):
    def invalid_constant(value):
        fail(str(path), f"nonfinite JSON number {value}")
    return json.loads(read_text(path, 32 * 1024 * 1024),
                      object_pairs_hook=unique_pairs, parse_constant=invalid_constant)


class UniqueSafeLoader(yaml.SafeLoader):
    pass


def yaml_mapping(loader, node):
    pairs = []
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            fail("YAML", "mapping keys must be strings; merge keys are unsupported")
        pairs.append((key, loader.construct_object(value_node, deep=True)))
    return unique_pairs(pairs)


UniqueSafeLoader.add_constructor("tag:yaml.org,2002:map", yaml_mapping)


def read_yaml(path):
    text = read_text(path, 1024 * 1024)
    # Aliases can expand exponentially or create recursive structures.
    for token in yaml.scan(text):
        if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
            fail(str(path), "YAML anchors and aliases are unsupported")
    return yaml.load(text, Loader=UniqueSafeLoader)
