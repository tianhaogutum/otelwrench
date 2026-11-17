import base64
from jsonpointer import JsonPointer

import pytest

from otelwrench.privacy.collector import run_stage
from otelwrench.privacy.evidence import find_text, type_coverage, view
from otelwrench.privacy.inputs import sample_messages


def test_type_named_attribute_key_does_not_invent_a_value_type():
    document = {"attributes": {"arrayValue": {"intValue": "42"}}}
    result = type_coverage(document, document)
    rows = {row["type"]: row for row in result["types"]}
    assert rows["arrayValue"]["input_locations"] == 0
    assert rows["intValue"]["input_locations"] == 1


@pytest.mark.integration
def test_real_sdk_complex_body_exercises_all_anyvalue_types(collector, root, local_tmp):
    messages, kind = sample_messages(root / "examples/privacy/logs-complex.pb", "logs")
    actual = run_stage(collector, "logs", messages, kind, local_tmp / "complex", None)
    document = view(actual)
    result = type_coverage(document, document)
    assert result["unexercised_input_types"] == []
    body = document["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["body"]["kvlistValue"][
        "values"
    ]
    assert body["binary"]["bytesValue"] == base64.b64encode(b"SYNTHETIC-SECRET").decode()
    assert body["flag"]["boolValue"] is True
    assert body["integer"]["intValue"] == "4111111111111111"
    assert body["not_a_number"]["doubleValue"] == "NaN"
    assert body["positive_infinity"]["doubleValue"] == "Infinity"
    assert body["negative_infinity"]["doubleValue"] == "-Infinity"
    unusual = [row for row in find_text(document, "SYNTHETIC-SECRET") if "\n" in row["pointer"]]
    assert unusual
    assert JsonPointer(unusual[0]["pointer"]).resolve(document) == "SYNTHETIC-SECRET"


def test_sdk_fixture_does_not_embed_the_generator_host_path(root):
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

    request = ExportLogsServiceRequest()
    request.ParseFromString((root / "examples/privacy/logs.pb").read_bytes())
    for resource in request.resource_logs:
        for scope in resource.scope_logs:
            for record in scope.log_records:
                attrs = {item.key: item.value for item in record.attributes}
                assert attrs["code.file.path"].string_value == "/synthetic/checkout.py"
