# Third-party sources and attribution

otelwrench is licensed under Apache-2.0 (see [LICENSE](LICENSE) and [NOTICE](NOTICE)). The
commands below were previously separate Apache-2.0 projects by the same authors; their
notices are preserved here, one section per command, with paths updated to this
repository's layout. Dependencies are installed by your package manager under their own
licenses and are never vendored; the otelwrench wheel contains only the `otelwrench`
package and these project notices. License texts that must accompany redistribution are in
[licenses/](licenses/).

Dependency versions quoted below are the ones each tool was originally locked and
validated against; otelwrench itself declares version ranges in `pyproject.toml`.
Product names identify interoperable formats and APIs only; otelwrench is not affiliated
with or endorsed by OpenTelemetry, Prometheus, Grafana Labs (Loki, Mimir, Grafana), Sentry,
Jaeger, Zipkin or any other project named here.

## At a glance

### Runtime dependencies (installed separately, never vendored)

| Package | License | Used by |
| --- | --- | --- |
| [prometheus-client](https://github.com/prometheus/client_python) (validated: 0.25.0) | Apache-2.0 AND BSD-2-Clause (bundled `decorator` 4.0.10 by Michele Simionato; [NOTICE](licenses/prometheus-client-NOTICE.txt), [decorator license](licenses/decorator-BSD-2-Clause.txt)) | `prom-names` (exposition self-check), `promdiff` and `preflight` (exposition parser); tests of `spanmetrics` and the tracepack recipe; the `preflight` benchmark's syntax-only baseline |
| [PyYAML](https://github.com/yaml/pyyaml) (validated: 6.0.3) | MIT ([text](licenses/PyYAML-MIT.txt)); optional bundled LibYAML 0.2.5 in binary wheels: MIT ([text](licenses/LibYAML-MIT.txt)) | `loki` (mapping profiles), `tailsim` (sampling policies; uses the pure-Python SafeLoader), `preflight` (Prometheus rule files given to `--queries`; `safe_load_all`) |
| [google-re2](https://github.com/google/re2) (validated: 1.1.20251105) | BSD-3-Clause ([text](licenses/google-re2-BSD-3-Clause.txt)) | `loki` (RE2 attribute selectors, matching Loki's regex semantics) |

Optional, `privacy` extra (`pip install 'otelwrench[privacy]'`; also in the `test` extra):

| Package | License | Used by |
| --- | --- | --- |
| [DeepDiff](https://github.com/seperman/deepdiff) (validated: 8.6.1) | MIT ([text](licenses/deepdiff-MIT.txt)) | `privacy` (structural diffs and typed equality of Collector output) |
| [python-json-pointer](https://github.com/stefankoegl/python-json-pointer) (`jsonpointer`, validated: 3.0.0) | BSD-3-Clause ([text](licenses/jsonpointer-BSD-3-Clause.txt)) | `privacy` (RFC 6901 location addresses and `pointer:` expectations) |
| [jsonpath-ng](https://github.com/h2non/jsonpath-ng) (validated: 1.7.0) | Apache-2.0 | `privacy` (JSONPath expectations) |
| [opentelemetry-proto](https://github.com/open-telemetry/opentelemetry-python) (validated: 1.38.0) and its dependency [protobuf](https://github.com/protocolbuffers/protobuf) | Apache-2.0; protobuf BSD-3-Clause ([text](licenses/protobuf-BSD-3-Clause.txt)) | `privacy` (official generated OTLP classes for ingestion audits and OTLP/protobuf responses) |

`privacy` also executes an [OpenTelemetry Collector Contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib)
binary (`otelcol-contrib`, Apache-2.0) that you supply or explicitly download with
`otelwrench privacy install-collector`. It is never bundled with otelwrench, never downloaded at
install time, and runs unmodified as a separate process.

`sentry`, `spanmetrics`, `cardinality`, `tracepack`, `tracediff` and `alert-flapsim` use only
the Python standard library at runtime. Dependency wheels may include their own native
components and notices; retain those notices if you redistribute the wheels or an installed
environment.

Test-only dependencies (`test` extra) are [pytest](https://github.com/pytest-dev/pytest) (MIT)
and its dependencies (iniconfig, pluggy: MIT; packaging: Apache-2.0 OR BSD-2-Clause; Pygments:
BSD-2-Clause). Build-only tools are [hatchling](https://github.com/pypa/hatch) (MIT) and
[hatch-fancy-pypi-readme](https://github.com/hynek/hatch-fancy-pypi-readme) (MIT).

### Bundled code and data

- **No third-party source code is vendored.** Every module under `src/otelwrench/` is original
  Apache-2.0 work.
- `src/otelwrench/loki/defaults.json` is a dated list of the 18 default Loki resource-attribute
  names plus retrieval metadata, taken from Loki's public OTLP documentation
  (`docs/sources/shared/otel.md`, retrieved 2026-11-11, SHA-256
  `be9d44ae00e13ed3773441aaf4e05bfcb6a367715eef7c5561f5c95d30a788d3`). It contains attribute-name
  facts only, no Loki source.
- All test, example and benchmark fixtures are original synthetic data under Apache-2.0. The
  `privacy` fixtures (`examples/privacy/*.pb`, `*.json`, `*.jsonl`) were produced from wholly
  synthetic values by the OpenTelemetry Python SDK and Collector 0.72.0 file exporter; see
  [examples/privacy/PROVENANCE.md](examples/privacy/PROVENANCE.md).

### Format and behavior references (not dependencies, no code copied)

| Reference | License | Used for |
| --- | --- | --- |
| [OpenTelemetry Protocol](https://github.com/open-telemetry/opentelemetry-proto) | Apache-2.0 | OTLP JSON envelopes, IDs, AnyValue, enums and timestamps (all OTLP commands) |
| [OpenTelemetry Collector Contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib) | Apache-2.0 | Prometheus naming (`prom-names`), spanmetrics connector approach (`spanmetrics`), tail-sampling processor configuration and decision flow (`tailsim`), value stringification (`loki`), Sentry receiver context (`sentry`) |
| [Prometheus](https://github.com/prometheus/prometheus) and the [exposition formats](https://prometheus.io/docs/instrumenting/exposition_formats/) | Apache-2.0 | Text format 0.0.4, label identity and `metric_relabel_configs` semantics, alerting `for`/`keep_firing_for`, HTTP API matrix JSON, rule-file layout (`groups`/`rules`/`expr`, read by `preflight --queries`) |
| [Grafana](https://github.com/grafana/grafana) dashboard JSON model (public documentation) | AGPL-3.0-only (project) | `preflight --queries` reads `expr` strings and panel titles from exported dashboard JSON; no Grafana source is copied, linked or executed |
| [Grafana Loki](https://github.com/grafana/loki) documentation | AGPL-3.0-only (project) | OTLP mapping configuration and push JSON format; only public documentation is referenced, no AGPL source or binaries are copied, linked, downloaded or executed |
| [Sentry Python SDK](https://github.com/getsentry/sentry-python) | MIT | Envelope and event formats (`sentry`) |
| [Jaeger](https://github.com/jaegertracing/jaeger) | Apache-2.0 | Query JSON trace/process/reference structure (`tracepack`, `tracediff`) |
| [Zipkin](https://github.com/openzipkin/zipkin) / [zipkin-api](https://github.com/openzipkin/zipkin-api) | Apache-2.0 | Zipkin v2 spans (`tracepack`) |

Sentry **server** is source-available under FSL-1.1-Apache-2.0, distinct from the MIT SDK, and
Loki, Mimir and Grafana are AGPL-3.0. No Sentry server, Loki, Mimir or Grafana source is copied
or used as a dependency by any command.

## Contents

- [`otelwrench sentry` (formerly sentry2otlp)](#otelwrench-sentry-formerly-sentry2otlp)
- [`otelwrench loki` (formerly otlp2loki-preview)](#otelwrench-loki-formerly-otlp2loki-preview)
- [`otelwrench prom-names` (formerly otlp-prom-namecheck)](#otelwrench-prom-names-formerly-otlp-prom-namecheck)
- [`otelwrench spanmetrics` (formerly spanmetrics-offline)](#otelwrench-spanmetrics-formerly-spanmetrics-offline)
- [`otelwrench promdiff` (formerly promdiff-guard)](#otelwrench-promdiff-formerly-promdiff-guard)
- [`otelwrench tailsim` (formerly tailsim)](#otelwrench-tailsim-formerly-tailsim)
- [`otelwrench cardinality` (formerly promcard-trim)](#otelwrench-cardinality-formerly-promcard-trim)
- [`otelwrench tracepack`, `otelwrench tracediff`, `otelwrench alert-flapsim` (Node.js originals)](#otelwrench-tracepack-otelwrench-tracediff-otelwrench-alert-flapsim-nodejs-originals)
- [`otelwrench preflight` (new in otelwrench)](#otelwrench-preflight-new-in-otelwrench)
- [`otelwrench privacy` (formerly otel-privacy-replay)](#otelwrench-privacy-formerly-otel-privacy-replay)

## `otelwrench sentry` (formerly sentry2otlp)

The converter and synthetic fixtures are original code. There are no third-party runtime or
test package dependencies; Python's standard library is supplied by the user's Python
installation (PSF license). The original build backend was setuptools 80.9.0 (MIT).

| Reference | Upstream license |
| --- | --- |
| Sentry Python SDK | MIT |
| OpenTelemetry Protocol | Apache-2.0 |
| OpenTelemetry Collector Contrib | Apache-2.0 |

The implementation uses public envelope and OTLP formats; no SDK or Collector source
implementation was copied, so no upstream code NOTICE is required. The original NOTICE reads:

> sentry2otlp — Original interoperability implementation of public Sentry SDK envelope and
> OpenTelemetry Protocol JSON formats. Not affiliated with or endorsed by Sentry or
> OpenTelemetry. No upstream implementation code is included.

## `otelwrench loki` (formerly otlp2loki-preview)

The original implementation is Apache-2.0. No Loki, Grafana, Mimir, Sentry server, Collector or
OTLP implementation source has been copied into it. Names and format facts are used for
interoperability.

| Distribution | Validated version | License | Use |
| --- | --- | --- | --- |
| PyYAML | 6.0.3 | MIT ([text](licenses/PyYAML-MIT.txt), copied verbatim from the installed wheel's `pyyaml-6.0.3.dist-info/licenses/LICENSE`) | Safe YAML parsing |
| google-re2 | 1.1.20251105 | BSD-3-Clause ([text](licenses/google-re2-BSD-3-Clause.txt), copied verbatim from `google_re2-1.1.20251105.dist-info/licenses/LICENSE`) | RE2 regular expressions, avoiding Python-only look-around/backreferences |

References only:

- **OpenTelemetry Protocol** (Apache-2.0): log/AnyValue schemas and JSON encoding facts. No proto
  source, generated bindings or SDK code is bundled.
- **OpenTelemetry Collector / Collector Contrib** (Apache-2.0): public value-stringification
  behavior as a format reference only; serialization code here is original.
- **Grafana Loki** (AGPL-3.0-only): only public mapping/configuration and push-format
  documentation is referenced. The bundled dated list (`src/otelwrench/loki/defaults.json`)
  consists of attribute-name facts plus retrieval metadata, not Loki source.

The original build backend was uv_build 0.11.19 (MIT OR Apache-2.0), used only for packaging.

## `otelwrench prom-names` (formerly otlp-prom-namecheck)

The converter and CLI are an original interoperability implementation; no upstream
translator/server implementation was copied.

| Material | Role | License |
| --- | --- | --- |
| [prometheus-client 0.25.0](https://github.com/prometheus/client_python) | Runtime dependency; real exposition parsing and validation | Apache-2.0; NOTICE retained in [licenses/prometheus-client-NOTICE.txt](licenses/prometheus-client-NOTICE.txt) |
| decorator 4.0.10, Michele Simionato | Vendored inside prometheus-client; not a separately installed dependency | BSD-2-Clause terms preserved verbatim in [licenses/decorator-BSD-2-Clause.txt](licenses/decorator-BSD-2-Clause.txt), extracted from `prometheus_client/decorator.py` |
| [OpenTelemetry Collector Contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib) | Naming behavior reference only | Apache-2.0 |
| [OpenTelemetry Protocol](https://github.com/open-telemetry/opentelemetry-proto) | OTLP JSON format reference only | Apache-2.0 |
| [Prometheus](https://github.com/prometheus/prometheus) | Text format and naming reference only | Apache-2.0 |

The original NOTICE preserved the installed prometheus-client NOTICE, including its bundled
decorator attribution; the root [NOTICE](NOTICE) carries it forward. The archived Collector
translator README describes normalization and specifically says naming does not rescale values.
The original build backend was hatchling 1.29.0 (MIT).

## `otelwrench spanmetrics` (formerly spanmetrics-offline)

An original interoperability implementation with no runtime dependencies; it does not vendor
upstream implementation code.

- **OpenTelemetry Protocol** (Apache-2.0): OTLP JSON envelope, IDs, typed attributes, integer
  enums and timestamps.
- **OpenTelemetry Collector Contrib spanmetrics connector** (Apache-2.0): existing online
  spanmetrics approach and duration histograms. No connector source is copied; full connector
  equivalence is not claimed.
- **prometheus-client 0.25.0** (Apache-2.0): used only by tests and the tracepack recipe for
  independent Prometheus exposition parsing; not a runtime dependency of this command.

The original build backend was hatchling 1.29.0 (MIT).

## `otelwrench promdiff` (formerly promdiff-guard)

The implementation is original; there is no vendored third-party implementation code.

| Material | Role | License |
| --- | --- | --- |
| `prometheus-client` 0.25.0 | Runtime dependency; exposition parsing | Apache-2.0 AND BSD-2-Clause; upstream [client_python v0.25.0](https://github.com/prometheus/client_python/tree/v0.25.0) |
| Prometheus exposition documentation / promtool | Format and comparison reference only | Apache-2.0 |

The installed client's NOTICE names its bundled `decorator` 4.0.10 by Michele Simionato
(BSD-2-Clause). Its upstream NOTICE is retained verbatim in
[licenses/prometheus-client-NOTICE.txt](licenses/prometheus-client-NOTICE.txt) and the bundled
decorator's copyright, license conditions and disclaimer in
[licenses/decorator-BSD-2-Clause.txt](licenses/decorator-BSD-2-Clause.txt). No decorator
implementation is copied. The original NOTICE reads:

> promdiff-guard — This product depends on the Prometheus Python client 0.25.0
> (Apache-2.0 AND BSD-2-Clause). Its original NOTICE is retained in
> licenses/prometheus-client-NOTICE.txt; its bundled decorator license is in
> licenses/decorator-BSD-2-Clause.txt. Dependency code is installed separately and is not
> vendored in this project.

The original build backend was hatchling 1.30.1 (MIT; build dependencies packaging:
Apache-2.0 OR BSD-2-Clause, pathspec: MPL-2.0, pluggy: MIT, trove-classifiers: Apache-2.0).

## `otelwrench tailsim` (formerly tailsim)

Source, tests and synthetic fixtures are an original implementation under Apache-2.0. No
Collector source is vendored or translated.

| Material | Role | License |
| --- | --- | --- |
| OpenTelemetry Collector Contrib | Configuration/decision reference only | Apache-2.0 |
| OpenTelemetry Protocol | OTLP JSON format reference only | Apache-2.0 |
| PyYAML 6.0.3 | Runtime dependency; safe YAML parsing | MIT; [licenses/PyYAML-MIT.txt](licenses/PyYAML-MIT.txt) ([upstream](https://github.com/yaml/pyyaml/blob/6.0.3/LICENSE)) |
| LibYAML 0.2.5 | Optional native component bundled in PyYAML binary wheels; this command uses the Python SafeLoader | MIT; [licenses/LibYAML-MIT.txt](licenses/LibYAML-MIT.txt) ([upstream](https://github.com/yaml/libyaml/blob/0.2.5/License)) |

Upstream references checked on 2026-11-11:

- [Collector configuration types](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/tailsamplingprocessor/config.go)
- [Decision flow](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/tailsamplingprocessor/README.md#policy-decision-flow)
- [Latency boundary behavior](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/tailsamplingprocessor/internal/sampling/latency.go)
- [NOT behavior](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/tailsamplingprocessor/internal/sampling/not.go)
- [DROP behavior](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/tailsamplingprocessor/internal/sampling/drop.go)
- [OTLP JSON encoding](https://github.com/open-telemetry/opentelemetry-proto/blob/main/docs/specification.md#json-protobuf-encoding)

These are compatibility references, not a claim to emulate every Collector version. The
explicit complete-trace semantics take precedence over runtime buffering and
incremental-evaluation behavior. The original NOTICE reads:

> Tailsim — Original offline interoperability implementation, licensed under Apache-2.0.
> OpenTelemetry is a format/configuration reference; this tool is not endorsed by or part of
> OpenTelemetry. The MIT license for PyYAML is preserved in licenses/PyYAML-MIT.txt. The
> optional LibYAML component in PyYAML wheels is MIT licensed; see licenses/LibYAML-MIT.txt.

The original build backend was setuptools 80.9.0 (MIT).

## `otelwrench cardinality` (formerly promcard-trim)

An original interoperability implementation with no third-party runtime dependencies and no
vendored upstream code.

- **Prometheus** (Apache-2.0): [configuration reference](https://github.com/prometheus/prometheus/blob/main/docs/configuration/configuration.md)
  for canonical label identity and metric relabel rule semantics.
- **Prometheus Python client** (Apache-2.0): exposition format context only; this command does
  not import or copy its parser.
- [Prometheus text format reference](https://prometheus.io/docs/instrumenting/exposition_formats/).
  Compatibility is the explicit subset documented in [docs/cardinality.md](docs/cardinality.md).

No Prometheus source code or attribution-bearing implementation has been copied, so no upstream
implementation NOTICE is required. The original build backend was setuptools 80.9.0 (MIT).

## `otelwrench tracepack`, `otelwrench tracediff`, `otelwrench alert-flapsim` (Node.js originals)

These commands are Python ports of original Apache-2.0 Node.js tools that had no npm runtime or
development dependencies (Node.js was a separately installed runtime and was never vendored); the
ports use only the Python standard library. No upstream server, SDK, generated protobuf or
translator code has been copied. The original projects' Apache-2.0 `LICENSE` files contained the
standard license text, the same text as this repository's [LICENSE](LICENSE).

| Reference | License | Used by |
| --- | --- | --- |
| [OpenTelemetry Protocol](https://github.com/open-telemetry/opentelemetry-proto) ([specification](https://github.com/open-telemetry/opentelemetry-proto/blob/main/docs/specification.md), [trace.proto](https://github.com/open-telemetry/opentelemetry-proto/blob/main/opentelemetry/proto/trace/v1/trace.proto)) | Apache-2.0 | `tracepack`, `tracediff` |
| [Jaeger](https://github.com/jaegertracing/jaeger) ([v1.57.0 JSON model](https://github.com/jaegertracing/jaeger/blob/v1.57.0/model/json/model.go), [Jaeger UI trace types](https://github.com/jaegertracing/jaeger-ui/blob/main/packages/jaeger-ui/src/types/trace.ts)) | Apache-2.0 | `tracepack`, `tracediff` |
| [Zipkin v2 API schema, pinned at 069ec8d1c7b58578d338b39f60abb211b7212618](https://github.com/openzipkin/zipkin-api/blob/069ec8d1c7b58578d338b39f60abb211b7212618/zipkin2-api.yaml) | Apache-2.0 | `tracepack` |
| [Prometheus](https://github.com/prometheus/prometheus) HTTP API matrix JSON and alerting `for` / `keep_firing_for` behavior, as described in the [alerting rules documentation](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/) and [promtool](https://prometheus.io/docs/prometheus/latest/command-line/promtool/) references | Apache-2.0 | `alert-flapsim` |

The schema and documentation references above were consulted on 2026-11-11. They explain the
supported subsets; none of these tools is certified or endorsed by those projects, and no
upstream NOTICE applies because no upstream implementation source was copied.

The original NOTICE files read:

> tracepack — Copyright 2026 tracepack contributors. Original interoperability
> implementation. Upstream project names describe compatible file formats and do not imply
> endorsement. No upstream code is reused.

> tracediff — Copyright 2026 tracediff contributors. Original interoperability
> implementation. No upstream implementation code is included. OpenTelemetry Protocol and
> Jaeger are format references.

> alert-flapsim — Copyright 2026 alert-flapsim contributors. Original interoperability
> implementation. Prometheus is a format and behavioral reference; no Prometheus
> implementation code is incorporated.

The original NOTICE files also pointed to license evidence (the Apache-2.0 license texts of
OpenTelemetry, Jaeger, Zipkin and Prometheus) kept in each project's research archive. Those
archives were planning material, not part of the tools, and are not carried over; the license
of each reference is listed in the table above. All fixtures used by these commands are
synthetic and generated locally; no captured production data or upstream source snippets
are included.

## `otelwrench preflight` (new in otelwrench)

Original Apache-2.0 code written for otelwrench. It has no rules of its own beyond glue: it
calls the `promdiff` parser, comparison and rename projection, the `prom-names` naming
function (`metric_name`) and the `cardinality` parser, summary and label-removal analysis,
so the references and dependencies of those three commands apply (prometheus-client through
the `promdiff` parser; PyYAML for rule files given to `--queries`). Prometheus rule files and
Grafana dashboard JSON are read as file formats only. The example dashboard, rule file and
`/metrics` captures in `examples/preflight/` describe a fictional service and are original
synthetic data.

## `otelwrench privacy` (formerly otel-privacy-replay)

An Apache-2.0 integration adapter, previously a separate project by the same authors. It wraps
an unmodified, version- and SHA-256-pinned OpenTelemetry Collector Contrib executable: all
OTLP parsing, processor execution (`redaction`, `attributes`, `resource`, `transform`,
`filter`, `batch`) and serialization happen in that binary. No Collector source is copied,
translated or reimplemented. The adapter's own code generates a loopback-only Collector
configuration, compares outputs and evaluates expectations.

| Material | Role | License |
| --- | --- | --- |
| [OpenTelemetry Collector Contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib) `otelcol-contrib` (pinned: 0.72.0, darwin/arm64 archive SHA-256 `77777ea9aaa73b5ffb0cf7fef689fdf9826dca1ccaed9cf9666135b38ad06e70`) | Executed as a separate process; supplied by the user or downloaded on request by `install-collector` from the official [releases](https://github.com/open-telemetry/opentelemetry-collector-releases); never bundled | Apache-2.0 |
| [PyYAML](https://github.com/yaml/pyyaml) 6.0.3 | Runtime dependency; safe YAML loading with duplicate-key and bounded-alias guards added by the adapter | MIT; [licenses/PyYAML-MIT.txt](licenses/PyYAML-MIT.txt) |
| [jsonpath-ng](https://github.com/h2non/jsonpath-ng) 1.7.0 | `privacy` extra; JSONPath expectations | Apache-2.0 |
| [python-json-pointer](https://github.com/stefankoegl/python-json-pointer/tree/v3.0.0) 3.0.0 | `privacy` extra; RFC 6901 addresses | BSD-3-Clause; [licenses/jsonpointer-BSD-3-Clause.txt](licenses/jsonpointer-BSD-3-Clause.txt), copied verbatim from `jsonpointer-3.0.0.dist-info/LICENSE.txt` |
| [DeepDiff](https://pypi.org/project/deepdiff/8.6.1/) 8.6.1 | `privacy` extra; structural diffs and typed equality | MIT; [licenses/deepdiff-MIT.txt](licenses/deepdiff-MIT.txt), copied verbatim from `deepdiff-8.6.1.dist-info/licenses/LICENSE` |
| [opentelemetry-proto](https://github.com/open-telemetry/opentelemetry-python/releases/tag/v1.38.0) 1.38.0 and protobuf | `privacy` extra; generated OTLP classes for ingestion audits and protobuf responses | Apache-2.0; protobuf BSD-3-Clause ([licenses/protobuf-BSD-3-Clause.txt](licenses/protobuf-BSD-3-Clause.txt)) |
| OpenTelemetry Python SDK 1.38.0 (`opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`) | Used only by `examples/privacy/generate_fixtures.py` to produce the protobuf fixtures; not an otelwrench dependency | Apache-2.0 |

Prior art surveyed before the tool was built (Collector redaction processor, file exporter,
testbed and telemetrygen, OTelBin, base14 Scout `config test`, Telflo pipeline tests) and the
scope decision are kept in [docs/privacy/prior-art.md](docs/privacy/prior-art.md). The
original project had no NOTICE file; its Apache-2.0 `LICENSE` contained the standard license
text, the same text as this repository's [LICENSE](LICENSE). The original build backend was
hatchling 1.27+ (MIT).
