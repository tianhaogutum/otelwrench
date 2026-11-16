# otelwrench documentation

One page per command. Each page starts with the problem the command solves and a quick
start, followed by the full reference. Run `otelwrench <command> --help` for the flags.
Quick starts use the sample inputs under [`examples/`](../examples/), so run them from a
clone of the repository (`git clone https://github.com/tianhaogutum/otelwrench`).

## Metrics

| Command | Use it to |
| --- | --- |
| [`otelwrench preflight`](preflight.md) | One CI verdict on a before/after `/metrics` pair: OTLP suffix renames, removed families, type and label changes, series growth, and the dashboard panels and alerts each one breaks, with the follow-up command per finding |
| [`otelwrench prom-names`](prom-names.md) | See what OTLP gauge/sum metric and label names become in Prometheus, including unit suffixes and name collisions |
| [`otelwrench promdiff`](promdiff.md) | Compare two `/metrics` snapshots and fail CI on breaking schema changes (removed families, label or type changes, series growth) |
| [`otelwrench cardinality`](cardinality.md) | Check whether dropping labels from saved scrapes would merge distinct series, and emit a candidate relabel rule when it is safe |

## Traces

| Command | Use it to |
| --- | --- |
| [`otelwrench tracepack`](tracepack.md) | Convert saved traces between OTLP, Jaeger and Zipkin JSON with explicit loss reports, and pack them into hash-verified, replayable bundles |
| [`otelwrench tracediff`](tracediff.md) | Diff two saved OTLP/Jaeger trace corpora by operation latency and parent/child topology, and gate the result with CI budgets |
| [`otelwrench spanmetrics`](spanmetrics.md) | Derive the RED metrics (`spanmetrics_*`) a set of saved traces produces, and compare two dimension choices before changing the connector |
| [`otelwrench tailsim`](tailsim.md) | Replay tail-sampling policies on saved OTLP traces, with an optional arrival-time model, and compare what two policies keep |

## Logs

| Command | Use it to |
| --- | --- |
| [`otelwrench loki`](loki.md) | Preview how Loki maps OTLP log attributes to stream labels, structured metadata or drops, and compare two mapping profiles |

## Alerts

| Command | Use it to |
| --- | --- |
| [`otelwrench alert-flapsim`](alert-flapsim.md) | Replay alert rules over a saved Prometheus `query_range` matrix and compare thresholds and `for`/`keep_firing_for` choices |

## Migration

| Command | Use it to |
| --- | --- |
| [`otelwrench sentry`](sentry.md) | Convert saved Sentry SDK envelopes or event JSON into OTLP JSON logs and traces with a complete loss manifest |

Additional reference: [Verifying saved tailsim reports](tailsim/verification.md).
Reproducible experiments live in [`benchmarks/`](../benchmarks/) (`preflight`, `promdiff`, `tailsim`, `cardinality`).
Third-party attribution is in [THIRD_PARTY.md](../THIRD_PARTY.md).

## Former tool names

| Former tool | Now |
| --- | --- |
| `sentry2otlp` | `otelwrench sentry` |
| `otlp2loki-preview` | `otelwrench loki` |
| `otlp-prom-namecheck` | `otelwrench prom-names` |
| `spanmetrics-offline` | `otelwrench spanmetrics` |
| `promdiff-guard` | `otelwrench promdiff` |
| `promcard-trim` | `otelwrench cardinality` |
| `tailsim` | `otelwrench tailsim` |
| `tracepack` (Node.js) | `otelwrench tracepack` |
| `tracediff` (Node.js) | `otelwrench tracediff` |
| `alert-flapsim` (Node.js) | `otelwrench alert-flapsim` |

Saved files keep their original formats: reports, bundles, evidence files and manifests
written by the former tools are read unchanged, and the tool names recorded inside them
(for example `tool.name: "sentry2otlp"` in a Sentry conversion manifest) are kept for
compatibility.
