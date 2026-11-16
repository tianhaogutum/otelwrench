# tracediff corpus-mutation experiment

This experiment checks that `otelwrench tracediff` ignores irrelevant capture
differences (trace/span IDs, ordering, OTLP versus Jaeger encoding) while still
detecting a known latency and topology mutation. All data are synthetic, generated with
fixed IDs and timestamps; no network, real traffic or secrets are involved. It is a
correctness and noise experiment, not a performance benchmark.

## Baseline and policy

The naive baseline is equality of the parsed documents after JSON serialization. It
marks all three candidates as changed; it is a change detector, not a latency gate.

[`baseline.json`](../../examples/tracediff/baseline.json) is OTLP JSON with 20 traces,
each a 100 ms `shop/checkout/server` root calling a 40 ms `database/select/client`
child; two roots have error status. So checkout has count 20, error fraction 0.1 and
min/median/p95/max 100 ms; select has count 20, error fraction 0 and 40 ms; the
cross-service checkout -> select edge has count 20; each capture has 40 unique spans.

Policy ([`budget.json`](../../examples/tracediff/budget.json)): `absoluteP95Ms=20`,
`relativeP95Percent=20`, `minSamples=20`, `failOnStructure=false`. Both thresholds
must be strictly exceeded.

## Results

Recorded in [`experiment-results.json`](../../examples/tracediff/experiment-results.json):

- `renamed.json`: the same OTLP data with new trace/span IDs and reversed resource and
  span order. Raw JSON differs; normalized structural changes 0, latency regressions 0,
  exit 0.
- `compatible.json`: an equivalent Jaeger query response with new IDs and reversed
  trace/span order. Raw JSON differs; structural changes 0, latency regressions 0,
  exit 0.
- `regression.json`: a Jaeger candidate that raises every checkout duration to 150 ms
  and adds a 5 ms `shop/audit/internal` child to each trace (60 spans in 20 traces).
  Checkout p95 delta 50 ms (+50%), one latency regression; one added operation and one
  added edge give 2 structural changes; the checkout -> select edge is unchanged; exit 1.

```sh
otelwrench tracediff examples/tracediff/baseline.json examples/tracediff/regression.json \
  --config examples/tracediff/budget.json --format json   # exit 1 is expected
```

## Independent verification

The oracle in `tests/tracediff/experiment.py` does not use the parser, normalizer,
comparator or fixture generator. It reads the committed OTLP and Jaeger records
directly, recomputes every group's count, errors and nearest-rank
min/median/p95/max (using integer ceiling division for ranks), and checks the exact
operation/edge deltas, the unchanged cross-service edge, the 50 ms / 50% gate and the
exit codes. The test suite also rebuilds every file in `examples/tracediff/` with
`tests/tracediff/generate_examples.py` and compares it with the committed copy, and
compares a fresh experiment run with `experiment-results.json`.

```sh
python -m pytest tests/tracediff
python -m tests.tracediff.experiment          # prints the results object
python -m tests.tracediff.generate_examples   # rewrites examples/tracediff/*.json
```

Canonical grouping intentionally aggregates repeated operations and excludes metadata;
the report's `diagnostics` disclose that projection. No runtime speed, memory or
production latency improvement is measured or claimed.
