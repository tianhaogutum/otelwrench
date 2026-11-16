# Reproducible retention experiment

Run from the repository root, with otelwrench installed (`pip install -e .`):

```sh
python benchmarks/tailsim/run.py
```

The script rewrites only `benchmarks/tailsim/traces.json` and `benchmarks/tailsim/results.json`
from deterministic synthetic data. It invokes the real `otelwrench tailsim compare` CLI twice. An independent
standard-library oracle (no imports from otelwrench) checks complete-trace duration,
child ERROR status, the exact sampled ID set, kept spans, changed traces,
lost error IDs, and filtered serialized UTF-8 byte counts. A failed check exits
nonzero. The fixture SHA-256 is recorded in results.json for reproduction.

Fixture: 100 traces / 200 spans. Each root is 10 ms OK; the child makes every
fourth trace 600 ms and every tenth trace ERROR. Every 25th trace has the
`/health` path. UTF-8 payloads vary deterministically. Baseline is
`examples/tailsim/baseline.yaml` (`always_sample`). The candidates are ERROR-or-slow,
and the same selection plus a health-path DROP veto. No random seed, capture,
credentials, or network is involved.

Actual results on 2026-11-11, CPython 3.12.13, PyYAML 6.0.3:

| Configuration | Kept traces | Kept spans | Error traces retained | Serialized bytes |
| --- | ---: | ---: | ---: | ---: |
| Always sample baseline | 100/100 | 200/200 | 10/10 | 66,470 |
| ERROR or >500 ms | 30/100 | 60/200 | 10/10 | 20,068 |
| Same + health DROP | 28/100 | 56/200 | 8/10 | 18,756 |

The second candidate loses error trace IDs 50 and 100 (hex IDs ending `32` and
`64`); the report makes this regression explicit. The unchanged baseline-to-
baseline interpretation is not substituted for candidate evaluation.

Byte measurements include resource/scope context and the JSON envelope, using
compact sorted-key UTF-8 with no newline. The original fixture is 153,109
bytes with indentation. Results are observations on this finite fixture only.
There is no speed benchmark, statistical confidence interval, production
performance improvement, backend-storage estimate, or cost forecast.
