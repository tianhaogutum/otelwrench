import json

from google.protobuf.internal.encoder import _VarintBytes
import pytest

from otelwrench.privacy.collector import run_stage
from otelwrench.privacy.evidence import view
from otelwrench.privacy.ingestion import audit_ingestion
from otelwrench.privacy.inputs import sample_messages


@pytest.mark.integration
def test_modern_log_field_loss_is_attributed_to_ingestion(collector, root, local_tmp):
    messages, kind = sample_messages(root / "examples/privacy/logs-modern.pb", "logs")
    baseline = run_stage(collector, "logs", messages, kind, local_tmp / "modern", None)
    audit = audit_ingestion(messages, kind, "logs", baseline)
    assert audit["status"] == "completed"
    assert any(row["value"] == "MODERN-EVENT-SECRET" for row in audit["removed_before_processors"])
    assert "MODERN-EVENT-SECRET" not in json.dumps(baseline)
    scope = view(baseline)["resourceLogs"][0]["scopeLogs"][0]["scope"]
    assert scope["attributes"]["scope.secret"]["stringValue"] == "SYNTHETIC-SECRET"


def test_unknown_wire_fields_are_explicit(root):
    messages, kind = sample_messages(root / "examples/privacy/traces.pb", "traces")
    data = messages[0] + _VarintBytes(999 << 3) + _VarintBytes(7)
    audit = audit_ingestion([data], kind, "traces", {"resourceSpans": []})
    assert audit["status"] == "completed"
    assert audit["unknown_protobuf_fields"]


def test_source_audit_has_its_own_memory_budget():
    audit = audit_ingestion([b"x" * (1024 * 1024 + 1)], "application/x-protobuf", "logs", {})
    assert audit["status"] == "unavailable"


def test_source_audit_bounds_json_nodes_across_request_boundaries():
    record = b'{"a":{"b":0}}'
    message = (
        b'{"resourceSpans":['
        + b",".join([record] * 17_000)
        + b"]}"
    )
    messages = [message] * 4
    assert sum(map(len, messages)) < 1024 * 1024

    audit = audit_ingestion(messages, "application/json", "traces", {"resourceSpans": []})

    assert audit["status"] == "unavailable"
    assert "analysis budget" in audit["reason"]


def test_missing_json_source_field_is_not_a_processor_hit():
    original = {
        "resourceLogs": [
            {
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "body": {"stringValue": "control"},
                                "futureField": "SECRET",
                            }
                        ]
                    }
                ]
            }
        ]
    }
    normalized = {
        "resourceLogs": [
            {
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "body": {"stringValue": "control"},
                            }
                        ]
                    }
                ]
            }
        ]
    }
    audit = audit_ingestion([json.dumps(original).encode()], "application/json", "logs", normalized)
    assert audit["removed_before_processors"][0]["value"] == "SECRET"
    assert "before any processor" in audit["removed_before_processors"][0]["attribution"]
