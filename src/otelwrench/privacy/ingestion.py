"""Separate decoding/normalization losses from actual processor changes."""

from __future__ import annotations

from typing import Any

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError, Message
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from .evidence import area, json_path, json_pointer, leaves, view
from .inputs import ReplayError, SIGNAL_KEYS, bound_document, strict_json


def _bounded_proto(message: Message) -> None:
    remaining = 30_000

    def visit(node: Message, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if depth > 48 or remaining < 0:
            raise ReplayError("protobuf semantic audit exceeds depth/node budget")
        for field, value in node.ListFields():
            if field.is_repeated:
                remaining -= len(value)
                if remaining < 0:
                    raise ReplayError("protobuf semantic audit exceeds node budget")
                if field.message_type is not None:
                    for item in value:
                        visit(item, depth + 1)
            elif field.message_type is not None:
                visit(value, depth + 1)

    visit(message, 0)


def audit_ingestion(
    messages: list[bytes], content_type: str, signal: str, normalized: dict
) -> dict:
    """Only diagnose missing source fields; ID encoding/default insertion aren't rule hits.

    The official generated protobuf codec supplies the semantic model. No wire
    decoder or redaction parser is recreated. Audit limits are deliberately lower
    than replay limits so dense protobuf inputs cannot expand without a bound.
    """
    if sum(map(len, messages)) > 1024 * 1024:
        return {
            "status": "unavailable",
            "reason": "source audit limited to 1 MiB; replay still executed",
        }
    key = SIGNAL_KEYS[signal]
    original: dict[str, Any] = {key: []}
    unknown_fields = False
    source_nodes = 0
    source_node_limit = 200_000
    request_type = {
        "traces": ExportTraceServiceRequest,
        "logs": ExportLogsServiceRequest,
        "metrics": ExportMetricsServiceRequest,
    }[signal]
    try:
        for data in messages:
            if content_type == "application/x-protobuf":
                message = request_type()
                message.ParseFromString(data)
                _bounded_proto(message)
                original_bytes = message.SerializeToString(deterministic=True)
                clean = request_type()
                clean.CopyFrom(message)
                clean.DiscardUnknownFields()
                unknown_fields |= original_bytes != clean.SerializeToString(deterministic=True)
                doc = MessageToDict(message, use_integers_for_enums=True)
            else:
                doc = strict_json(data)
                if key not in doc:
                    return {
                        "status": "unavailable",
                        "reason": "snake_case source audit is not supported",
                    }
            if source_nodes >= source_node_limit:
                raise ReplayError("source audit exceeds analysis budget (200000 nodes, depth 64)")
            source_nodes += bound_document(
                doc,
                max_nodes=source_node_limit - source_nodes,
                max_depth=64,
            )
            original[key].extend(doc.get(key, []))
        bound_document(normalized)
        source_view = view(original)
        old = dict(leaves(source_view))
        new = dict(leaves(view(normalized)))
    except (DecodeError, ReplayError, RecursionError) as exc:
        return {"status": "unavailable", "reason": str(exc)}
    removed = [
        {
            "path": json_path(parts),
            "pointer": json_pointer(parts),
            "area": area(parts),
            "value": value,
            "attribution": "removed during Collector ingestion/normalization, before any processor",
        }
        for parts, value in old.items()
        if parts not in new
    ]
    return {
        "status": "completed",
        "removed_before_processors": removed,
        "unknown_protobuf_fields": unknown_fields,
        "schema": "opentelemetry-proto Python 1.38.0",
        "scope": "missing known source fields only; not an equality check of OTLP encodings/defaults",
    }
