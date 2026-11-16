# Synthetic compatibility detection experiment for `otelwrench promdiff`

Run from the repository root with `python benchmarks/promdiff/run.py` (after
`pip install -e .`). `results.json` records an actual execution with
prometheus-client 0.25.0. `tests/promdiff/test_experiment.py` regenerates the
files, invokes `otelwrench promdiff` for all nine cases, checks a separately
specified oracle, and compares the result to this record (ignoring the recorded
prometheus-client version, since newer releases are allowed).
No seed, network access, production data, or wall-clock timing is involved.

Baseline: accept an upgrade when **both snapshots parse with prometheus_client**.
This is a defined single-snapshot syntax check, not promtool or a benchmark of
Prometheus server validation. All nine candidates are parseable, so this
baseline detects none of the four specified compatibility violations.

The input contains twenty gauge families with five labelled series apiece.
Each candidate is independently generated from that baseline. The four
violations are removal of one family, changing one gauge to counter, adding a
label key to one family, and increasing total series 25% against the default
20% budget. Five compatible cases exercise non-breaking changes.

| Mutation | Candidate series | Syntax parse | promdiff exit | promdiff status |
| --- | ---: | --- | ---: | --- |
| Values only | 100 | accepted | 0 | pass |
| Remove family | 95 | accepted | 1 | fail |
| Change type | 100 | accepted | 1 | fail |
| Add label key | 100 | accepted | 1 | fail |
| 25% series growth | 125 | accepted | 1 | fail |
| One missing observed series | 99 | accepted | 0 | warn |
| One changed label value | 100 | accepted | 0 | warn |
| HELP change | 100 | accepted | 0 | warn |
| Add one small family | 101 | accepted | 0 | warn |

Actual result: `otelwrench promdiff` detects all four specified violations, with zero false
violations among the five compatible cases. The syntax-only baseline detects
zero of four. This is evidence of detection on these explicit synthetic cases,
not market exclusivity, a runtime speedup, general precision/recall, or the
number of exporters/users affected in production.

To retain the generated inputs and CLI report artifacts, pass `--output-dir`
with a new directory. Existing directories are rejected to avoid overwriting
unrelated data. With no option, generated files are temporary and cleaned up.
