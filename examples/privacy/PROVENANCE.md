# Fixture provenance

`traces.pb`, `logs.pb`, `metrics.pb` are real `Export*ServiceRequest` protobufs
produced locally by OpenTelemetry Python SDK **1.38.0**, using its own OTLP
encoders in `generate_fixtures.py` (this directory). The SDK never exports over a network.
Values are wholly synthetic, including the Visa test card and `example.test`
addresses. Regenerate from the repository root with
`python examples/privacy/generate_fixtures.py` (needs `opentelemetry-sdk` and
`opentelemetry-exporter-otlp-proto-http` 1.38.0, which are not otelwrench dependencies).
IDs/timestamps vary when regenerated; tests do not assume their literal values.
The SDK logging handler adds source-code attributes. Its LogRecords explicitly
use `/synthetic/checkout.py` and synthetic function/line metadata, so fixtures do
not capture the generator's host home-directory path.
`logs-modern.pb` is another real SDK request with `event_name` and scope
attributes. The old Collector drops that event name during ingestion, while
retaining scope attributes; a regression test checks this separate attribution.

The span includes a real SDK resource, instrumentation scope, scalar/array/int
attributes, event, link and error status. Logs include both a structured map body
and a plain string body. Metrics include a sum and a histogram.
Scope attributes and an array containing both a marker and the test card extend
coverage beyond scalar record attributes.
`logs-complex.pb` is an SDK structured log body with all seven AnyValue types,
including nested maps, mixed arrays, bytes and NaN/positive/negative infinity.
The binary value remains base64 in OTLP JSON; literal marker scanning deliberately
does not pretend to decode it.

`traces.json`, `logs.json`, `metrics.json` and their `.jsonl` versions are genuine
Collector **0.72.0 file-exporter** output from the corresponding SDK protobufs.
Regenerate with `python examples/privacy/export_json_fixtures.py` after generating
protobufs and installing the pinned Collector (`otelwrench privacy install-collector`). Round-trip and multi-request tests use these exact upstream encodings.

`collector.yaml` follows the upstream redaction processor YAML format. Its
allowlist and ignored-key interaction are deliberate. With Collector 0.72.0,
redaction supports traces only. Requesting the logs/metrics pipelines must fail
explicitly, never claim those signals were scrubbed.
