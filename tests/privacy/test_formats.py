import json

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
import pytest

from otelwrench.privacy.collector import _check_response, run_stage
from otelwrench.privacy.evidence import count_records, differences, view
from otelwrench.privacy.inputs import ReplayError, sample_messages


@pytest.mark.integration
@pytest.mark.parametrize("signal", ["traces", "logs", "metrics"])
def test_actual_file_exporter_json_roundtrip(collector, root, local_tmp, signal):
    path = root / f"examples/privacy/{signal}.json"
    messages, kind = sample_messages(path, signal)
    result = run_stage(collector, signal, messages, kind, local_tmp / signal, None)
    assert not differences(view(json.loads(path.read_text())), view(result))


@pytest.mark.integration
@pytest.mark.parametrize("signal,expected", [("traces", 2), ("logs", 4), ("metrics", 4)])
def test_multiple_real_jsonl_requests_keep_all_records(
    collector, root, local_tmp, signal, expected
):
    data = (root / f"examples/privacy/{signal}.jsonl").read_bytes()
    path = local_tmp / f"{signal}.jsonl"
    path.write_bytes(data + data)
    messages, kind = sample_messages(path, signal)
    assert len(messages) == 2
    result = run_stage(collector, signal, messages, kind, local_tmp / signal, None)
    assert count_records(result, signal) == expected


@pytest.mark.parametrize("encoding", ["json", "protobuf"])
def test_partial_otlp_success_is_never_treated_as_complete(encoding):
    if encoding == "json":
        body = b'{"partialSuccess":{"rejectedSpans":"1","errorMessage":"rejected"}}'
    else:
        response = ExportTraceServiceResponse()
        response.partial_success.rejected_spans = 1
        body = response.SerializeToString()
    with pytest.raises(ReplayError, match="partial success"):
        _check_response(body, "application/" + encoding, "traces")


@pytest.mark.integration
def test_real_receiver_rejects_malformed_protobuf_and_stops(collector, local_tmp):
    with pytest.raises(ReplayError, match="rejected OTLP request"):
        run_stage(
            collector,
            "traces",
            [b"\x00invalid"],
            "application/x-protobuf",
            local_tmp / "invalid",
            None,
        )
