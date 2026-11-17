# otelwrench privacy: prior art and scope decision

Surveyed 2026-11-11 for the original standalone tool (otel-privacy-replay), now
`otelwrench privacy`. This is an integration adapter for the existing Collector,
not another redaction engine. Version-specific observations are labelled below.

## OpenTelemetry Collector Contrib redactionprocessor

- URLs: [security guidance](https://opentelemetry.io/docs/security/handling-sensitive-data/),
  [current README](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/processor/redactionprocessor),
  [v0.72.0 source](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.72.0/processor/redactionprocessor).
- Version: **0.72.0**, official darwin/arm64 release; archive SHA-256
  `77777ea9aaa73b5ffb0cf7fef689fdf9826dca1ccaed9cf9666135b38ad06e70`.
  Downloaded, checked `--version`, and replayed genuine SDK traces plus OTTL logs
  and metrics. The ignored-key and non-attribute observations are executable tests.
- Already does the core rule execution: key allowlists, ignored keys (which bypass
  checks), blocked value masking, and diagnostic summary attributes. Source shows
  resource and span attributes processed in 0.72.0, with other trace locations
  outside that traversal. Current main supports more signals and configuration.
- Remaining gap: a repeatable local privacy expectation check over **actual
  output**, including unchanged fields outside attributes. A valid configuration
  and processor diagnostics are not evidence that every location is covered.
- Reuse: invoke the unmodified executable; use its OTLP receiver and file
  exporter for canonical serialization, normalization, and processor execution.
- Constraint: newer official macOS contrib archives exceed the installer's 50 MiB download
  limit. 0.72.0 is 51,781,829 bytes (49.38 MiB), below 50 MiB. This old pin is a
  compatibility fixture, not a recommended production release. No claim about
  current Collector behavior follows from a run on this version.
- Also installed and ran **0.71.0**. It explicitly rejects `ignored_keys`; without it the allowlist removes that field
  and the card is masked. The default pin is still 0.72.0.

## Collector file exporter and OTLP receiver

- [file exporter v0.72.0](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.72.0/exporter/fileexporter),
  [OTLP specification](https://opentelemetry.io/docs/specs/otlp/).
- Version: 0.72.0 bundled components. Docs/source verified and exercised by
  executable replay.
- Already provide local ingestion of real OTLP JSON/protobuf and JSON-lines
  output. We do not invent a telemetry interchange format or parse debug text.
- Gap: no built-in before/after privacy assertions or per-stage inventory.

## Collector testbed and telemetrygen

- [testbed](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/testbed),
  [telemetrygen](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/cmd/telemetrygen).
- Version: main documentation inspected on survey date. **Not installed/run**:
  the survey used no Go toolchain or containers.
- Testbed already offers in-process/child-process runners, golden fixtures, mock
  backends and correctness validators. It is the better choice for a Go-based
  upstream processor test. Telemetrygen already generates all three signals.
- Gap: a small standalone local CLI for an engineer's YAML config and privacy
  expectations, without compiling the Collector testbed. We use SDK-generated
  OTLP fixtures instead of writing another synthetic telemetry generator.

## OTelBin (Dash0)

- [repository and README](https://github.com/dash0hq/otelbin).
- Version: main README inspected 2026-11-11; **not installed/run**.
- Already edits, visualizes, validates and shares Collector configurations.
  README describes Lambda-based distribution validation, not telemetry replay.
- Gap: actual retained telemetry and local privacy expectation assertions.

## base14 Scout `config test`

- [documentation](https://docs.base14.io/scout-cli/otel-config/config-test/),
  documentation updated 2026-04-09; **not installed/run** (docs-only verification).
- Already patches config, starts a local Collector, sends OTLP probes and reports
  pipeline pass/fail. Its isolated mode removes remote exporters. Generic local
  Collector probe execution is therefore already covered.
- Narrow remaining gap: location-level retention inventory, explicit expected
  absence/value checks, actual stage changes and ignored-key visibility. We do
  not claim to replace Scout's general configuration tooling.

## Telflo pipeline tests

- [documentation](https://docs.telflo.com/docs/cli/pipeline-tests/),
  updated 2026-09-11; **not installed/run** (docs-only verification).
- Already replays samples through a real version-pinned Collector and evaluates
  OTTL assertions. The core replay/assertion idea is already implemented there.
  Documentation describes submission to a server sandbox and token-based config
  access; that route is outside this command's local-only scope.
- Remaining gap addressed here: offline/local operation with no account, plus an
  automatic inventory of surviving attribute and non-attribute locations and
  stage-by-stage evidence. OTTL evaluation itself is left to upstream tools.

## Decision

Build a Python **thin wrapper** over a pinned, unmodified Collector. Only local
processor types are accepted; replace receivers/exporters with loopback OTLP and
local files. Run a normalization baseline and each processor in pipeline order.
Report exact changes, upstream summary attributes, observed bypass candidates,
unchanged locations, and user-authored JSONPath expectations. Do not infer
protection from the mere presence of a redaction component. Never silently skip
an unsupported processor or signal.

## Reused format/comparison libraries

- [PyYAML 6.0.3](https://pypi.org/project/PyYAML/6.0.3/) handles YAML using its safe
  loader, with duplicate-key and bounded-alias guards added by this adapter.
- [jsonpath-ng 1.7.0](https://github.com/h2non/jsonpath-ng) evaluates wildcard
  expectations. Verified by running it, including a reproduced newline-key
  round-trip limitation.
- [python-json-pointer 3.0.0](https://github.com/stefankoegl/python-json-pointer/tree/v3.0.0)
  generates/resolves exact RFC 6901 addresses, verified with quotes, slash, tilde,
  newline, backslash and Unicode names. This avoids inventing an address parser.
- [DeepDiff 8.6.1](https://pypi.org/project/deepdiff/8.6.1/) supplies structural
  diffs and typed equality. Actual attribute lists are normalized by their keys.
- [OpenTelemetry Python 1.38.0](https://github.com/open-telemetry/opentelemetry-python/releases/tag/v1.38.0)
  produces fixture protobufs; its generated protobuf classes handle semantic
  source auditing and OTLP responses. All were installed and run locally.
