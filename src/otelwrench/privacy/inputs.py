"""Small, bounded input adapters. OTLP decoding is left to the Collector."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


class ReplayError(Exception):
    """An actionable input or runtime failure."""


def bound_document(document: Any, max_nodes: int = 200_000, max_depth: int = 64) -> int:
    stack = [(document, 0)]
    visited = 0
    while stack:
        value, depth = stack.pop()
        visited += 1
        if visited > max_nodes or depth > max_depth:
            raise ReplayError(
                f"evidence document exceeds analysis budget ({max_nodes} nodes, depth {max_depth}); "
                "split the sample into smaller requests/runs"
            )
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
    return visited


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader: UniqueLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ReplayError("YAML mapping keys must be strings")
        if key in result:
            raise ReplayError(f"duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def bounded_read(path: Path, limit: int = 16 * 1024 * 1024) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ReplayError(f"{path}: input exceeds {limit} bytes")
    return data


def load_yaml(path: Path, data: bytes | None = None) -> dict[str, Any]:
    if data is None:
        data = bounded_read(path, 1024 * 1024)
    if len(data) > 1024 * 1024:
        raise ReplayError(f"{path}: YAML input exceeds 1 MiB")
    try:
        obj = yaml.load(data, Loader=UniqueLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise ReplayError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(obj, dict):
        raise ReplayError(f"{path}: expected a YAML mapping")
    _bounded_tree(obj)
    return obj


def _bounded_tree(value: Any) -> None:
    """Bound expanded aliases before dumping config or traversing expectations."""
    budget = 2 * 1024 * 1024

    def visit(node: Any, depth: int, ancestors: frozenset[int]) -> None:
        nonlocal budget
        budget -= 32 + (len(node.encode()) if isinstance(node, str) else 0)
        if depth > 64 or budget < 0:
            raise ReplayError("YAML expanded structure exceeds depth/size limits")
        if isinstance(node, (dict, list)):
            if id(node) in ancestors:
                raise ReplayError("recursive YAML aliases are not supported")
            ancestors = ancestors | {id(node)}
            children = list(node.items()) if isinstance(node, dict) else enumerate(node)
            for key, item in children:
                if isinstance(node, dict):
                    visit(key, depth + 1, ancestors)
                visit(item, depth + 1, ancestors)

    visit(value, 0, frozenset())


SIGNAL_KEYS = {"traces": "resourceSpans", "logs": "resourceLogs", "metrics": "resourceMetrics"}
LOCAL_PROCESSORS = frozenset(
    {"redaction", "attributes", "resource", "transform", "filter", "batch"}
)


def selected_pipeline(config: dict, pipeline: str) -> tuple[str, list[tuple[str, dict]]]:
    signal = pipeline.split("/", 1)[0]
    if signal not in SIGNAL_KEYS:
        raise ReplayError("pipeline must be traces[/name], logs[/name], or metrics[/name]")
    try:
        pipelines = config["service"]["pipelines"]
        definition = pipelines[pipeline]
    except (KeyError, TypeError) as exc:
        raise ReplayError(f"pipeline {pipeline!r} is not defined in service.pipelines") from exc
    if not isinstance(definition, dict):
        raise ReplayError(f"pipeline {pipeline!r} must be a mapping")
    names = definition.get("processors", [])
    if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
        raise ReplayError("pipeline processors must be a list of component IDs")
    if len(names) > 8 or len(set(names)) != len(names):
        raise ReplayError("use at most 8 distinct processors per replay pipeline")
    processors = config.get("processors")
    if processors is None:
        processors = {}
    if not isinstance(processors, dict):
        raise ReplayError("processors must be a mapping")
    selected = []
    for name in names:
        if name.split("/", 1)[0] not in LOCAL_PROCESSORS:
            raise ReplayError(
                f"unsupported processor {name!r}; supported local types: "
                + ", ".join(sorted(LOCAL_PROCESSORS))
            )
        if name not in processors:
            raise ReplayError(f"processor {name!r} is referenced but not defined")
        body = processors[name]
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise ReplayError(f"processor {name!r} configuration must be a mapping")
        try:
            encoded = json.dumps(body)
        except (ValueError, RecursionError, TypeError) as exc:
            raise ReplayError(f"processor {name!r} contains recursive or non-JSON YAML") from exc
        if "${" in encoded:
            raise ReplayError(
                f"processor {name!r}: resolve configuration providers/environment references "
                "into a local, self-contained configuration before replay"
            )
        selected.append((name, body))
    return signal, selected


def _unique_json(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReplayError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def strict_json(data: str | bytes) -> Any:
    def invalid_number(value: str) -> None:
        raise ReplayError(f"non-standard JSON number: {value}")

    return json.loads(data, object_pairs_hook=_unique_json, parse_constant=invalid_number)


def sample_messages(
    path: Path, signal: str, fmt: str = "auto", data: bytes | None = None
) -> tuple[list[bytes], str]:
    if data is None:
        data = bounded_read(path)
    if len(data) > 16 * 1024 * 1024:
        raise ReplayError("sample exceeds 16 MiB")
    if not data.strip():
        raise ReplayError("sample is empty")
    if fmt == "auto":
        fmt = "protobuf" if path.suffix.lower() in {".pb", ".bin", ".protobuf"} else "json"
    if fmt == "protobuf":
        return [data], "application/x-protobuf"
    try:
        obj = strict_json(data)
        docs = [obj]
    except json.JSONDecodeError:
        try:
            docs = [strict_json(line) for line in data.splitlines() if line.strip()]
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            raise ReplayError(f"invalid OTLP JSON or JSONL: {exc}") from exc
    except (UnicodeDecodeError, RecursionError) as exc:
        raise ReplayError(f"invalid OTLP JSON: {exc}") from exc
    if len(docs) > 1000:
        raise ReplayError("at most 1000 OTLP requests are supported per sample")
    key = SIGNAL_KEYS[signal]
    snake = {"traces": "resource_spans", "logs": "resource_logs", "metrics": "resource_metrics"}
    for doc in docs:
        if not isinstance(doc, dict) or not ({key, snake[signal]} & doc.keys()):
            raise ReplayError(f"sample must contain an OTLP {signal} request ({key})")
        if set(doc) - {key, snake[signal]}:
            raise ReplayError("OTLP request contains unexpected or mixed-signal top-level fields")
    return [json.dumps(doc, allow_nan=False).encode() for doc in docs], "application/json"
