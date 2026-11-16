# Reproducible observed-cardinality experiment

Run from the repository root, with otelwrench installed (`pip install -e .`):

```sh
python benchmarks/cardinality/experiment.py --routes 100 --pods 30 --repeats 3
```

The generator creates one actual Prometheus text scrape in a temporary directory.
It uses 100 routes × 2 methods × 30 pods = **6,000 series**, each with a unique
`debug_id`. There is no random seed: fixed nested loops determine every label
and value. Every even route has equal values across pods; every odd route varies
its values by pod. This gives both equal-value and different-value collisions.

To retain a generated scrape, explicitly supply a new path (existing files are
never overwritten):

```sh
python benchmarks/cardinality/experiment.py --routes 100 --pods 30 --repeats 3 --write-fixture /tmp/cardinality-generated.prom
```

The baseline retains every label. The safe plan deletes `debug_id`, retaining
route/method/pod uniqueness. The unsafe plan deletes `pod` and `debug_id`, merging
all 30 pods for each route/method. The oracle uses generator records and
`frozenset` identities, without importing the production parser, canonicalizer,
or analyzer. Each `otelwrench cardinality` subprocess parses the actual scrape and produces the full JSON
report. The experiment independently checks all original counts, every label's
values and cardinality, projected counts, safety, all collision relations, and
every original witness on **each** timed run. The experiment itself exits 0 when
the expected unsafe CLI exit 1 is verified.

## Recorded result

[results.json](results.json) was actually measured on 2026-11-11 at
19:13:19 UTC, Python 3.12.13, Linux x86_64 / glibc 2.43. Input is 516,886 bytes,
SHA-256 `c86181a4750467ede1fe18edd8b4c829d84edbdbce717e103b5295421df011da`.

| Plan | Original keys | Projected keys | Collision groups | Status | Median wall seconds (3 runs) |
| --- | ---: | ---: | ---: | --- | ---: |
| Baseline: keep all | 6,000 | 6,000 | 0 | baseline | 0.101619 |
| Delete debug_id | 6,000 | 6,000 | 0 | observed safe | 0.133002 |
| Delete pod,debug_id | 6,000 | 200 | 200 | **unsafe** | 0.221252 |

The unsafe plan has 100 equal-value and 100 different-value collision groups;
all 6,000 original series are included as witnesses. All 200 groups are unsafe.
The apparent 96.67% key reduction is **not valid ingestion or aggregation**.
The safe plan has **zero observed series reduction**. This is the intended
collision-aware result, not a failed opportunity to claim a saving.

Timings measure subprocess startup, parsing, analysis and full JSON output captured
in memory. They exclude independent verification. The baseline does less work and
the unsafe report has many more witnesses; these are not equivalent operations
for a speed comparison. These are single-machine illustrative measurements with
no warmup, memory measurement or statistical performance claim. There is no claim
of TSDB memory/cost reduction or production speedup.
