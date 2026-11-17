"""Produce genuine OTLP protobuf with OpenTelemetry Python SDK 1.38.0, offline.

Needs opentelemetry-sdk and opentelemetry-exporter-otlp-proto-http 1.38.0, which are not
otelwrench dependencies:

    python examples/privacy/generate_fixtures.py
"""

from pathlib import Path
import logging

from opentelemetry.exporter.otlp.proto.common import _log_encoder, metrics_encoder, trace_encoder
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, SpanContext, Status, StatusCode, TraceFlags


def main() -> None:
    out = Path(__file__).resolve().parent
    secret = "SYNTHETIC-SECRET"
    resource = Resource(
        {
            "service.name": "privacy-fixture",
            "owner.email": secret + "@example.test",
            "ignored_secret": secret,
        }
    )
    exporter = InMemorySpanExporter()
    traces = TracerProvider(resource=resource)
    traces.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = traces.get_tracer("fixture." + secret, "1.38.0", attributes={"scope.secret": secret})
    link = Link(
        SpanContext(123, 456, is_remote=True, trace_flags=TraceFlags(1)),
        {
            "link.secret": secret,
        },
    )
    with tracer.start_as_current_span("checkout " + secret, links=[link]) as span:
        span.set_attributes(
            {
                "card": "4111111111111111",
                "customer.email": secret + "@example.test",
                "ignored_secret": secret,
                "safe": "control",
                "integer_card": 4111111111111111,
                "array_secrets": [secret, "public", "4111111111111111"],
            }
        )
        span.add_event("event " + secret, {"event.secret": secret})
        span.set_status(Status(StatusCode.ERROR, "failed " + secret))
    traces.force_flush()
    (out / "traces.pb").write_bytes(
        trace_encoder.encode_spans(exporter.get_finished_spans()).SerializeToString()
    )
    traces.shutdown()

    log_exporter = InMemoryLogExporter()
    logs = LoggerProvider(resource=resource)
    logs.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    logger = logging.Logger("fixture.logs")
    logger.addHandler(LoggingHandler(logger_provider=logs))
    # LoggingHandler adds code.file.path automatically. Supply synthetic record
    # metadata so no developer home directory is captured by these fixtures.
    logger.handle(
        logger.makeRecord(
            logger.name,
            logging.WARNING,
            "/synthetic/checkout.py",
            42,
            {"card": "4111111111111111", "nested": {"secret": secret}},
            (),
            None,
            func="checkout",
            extra={"ignored_secret": secret, "safe": "control"},
        )
    )
    logger.handle(
        logger.makeRecord(
            logger.name,
            logging.ERROR,
            "/synthetic/checkout.py",
            43,
            "unstructured body " + secret,
            (),
            None,
            func="checkout",
        )
    )
    logs.force_flush()
    (out / "logs.pb").write_bytes(
        _log_encoder.encode_logs(log_exporter.get_finished_logs()).SerializeToString()
    )
    logs.get_logger("fixture.modern", "1.38.0", attributes={"scope.secret": secret}).emit(
        body="control",
        event_name="MODERN-EVENT-SECRET",
        attributes={"safe": "control"},
    )
    (out / "logs-modern.pb").write_bytes(
        _log_encoder.encode_logs(log_exporter.get_finished_logs()[-1:]).SerializeToString()
    )
    logs.get_logger("fixture.complex", "1.38.0").emit(
        body={
            "text": secret,
            "flag": True,
            "integer": 4111111111111111,
            "double": 1.5,
            "not_a_number": float("nan"),
            "positive_infinity": float("inf"),
            "negative_infinity": float("-inf"),
            "binary": b"SYNTHETIC-SECRET",
            "array": [secret, False, 7, {"nested.secret": secret}],
            "SYNTHETIC-SECRET-key": "control",
            "quote'\"/line\n~☃": secret,
        },
        attributes={"safe": "control"},
    )
    (out / "logs-complex.pb").write_bytes(
        _log_encoder.encode_logs(log_exporter.get_finished_logs()[-1:]).SerializeToString()
    )
    logs.shutdown()

    reader = InMemoryMetricReader()
    metrics = MeterProvider(resource=resource, metric_readers=[reader])
    meter = metrics.get_meter("fixture.metrics", "1.38.0", attributes={"scope.secret": secret})
    counter = meter.create_counter("checkout." + secret, description="count " + secret)
    counter.add(1, {"card": "4111111111111111", "ignored_secret": secret, "safe": "control"})
    histogram = meter.create_histogram("checkout.duration", unit="ms")
    histogram.record(12.5, {"customer.email": secret + "@example.test"})
    (out / "metrics.pb").write_bytes(
        metrics_encoder.encode_metrics(reader.get_metrics_data()).SerializeToString()
    )
    metrics.shutdown()
    print("wrote genuine SDK OTLP requests: traces/logs/metrics, modern log, complex log")


if __name__ == "__main__":
    main()
