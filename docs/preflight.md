# otelwrench preflight

An exporter, SDK or Collector upgrade can leave `/metrics` perfectly scrapeable and still
break dashboards and alerts: a histogram gains a `_milliseconds` suffix, a counter gains
`_total`, a unit moves from milliseconds to seconds, an attribute is renamed, a family
disappears, a counter becomes a gauge, a new label multiplies the series. Nothing errors;
panels go empty and alerts stop firing. `otelwrench preflight` reads a before and an after
capture and prints one verdict: what changed, which of your queries read it, and the exact
command to run next for each finding. It is read-only and offline.

## Quick start

```sh
pip install otelwrench
curl -s localhost:9464/metrics > before.prom      # capture, upgrade, capture again
curl -s localhost:9464/metrics > after.prom
otelwrench preflight before.prom after.prom
otelwrench preflight before.prom after.prom --queries dashboard.json --queries alerts.yaml
```

The bundled example is a fictional exporter upgrade (synthetic data, see
[`examples/preflight/`](../examples/preflight/)):

```sh
cd examples/preflight
otelwrench preflight before.prom after.prom --queries checkout-dashboard.json --queries checkout-alerts.yaml
```

It reports 8 errors and 2 warnings, and flags 9 of the 14 dashboard and alert queries,
which are exactly the 9 that the hand-written oracle `expected.json` marks as broken
(recorded in [`benchmarks/preflight/`](../benchmarks/preflight/)).

Exit status: `0` nothing at or above `--fail-on` (default `error`), `1` findings at or above
it, `2` usage or input error (unreadable file, invalid exposition, invalid query file).

## Input

- `BEFORE` and `AFTER`: Prometheus text format 0.0.4, as `/metrics` serves it, parsed with
  the strict [`promdiff`](promdiff.md#supported-subset-and-limitations) parser. OpenMetrics-only syntax (`# UNIT`,
  `# EOF`, exemplars) is an input error. The [`cardinality`](cardinality.md) parser reads the
  same files for label statistics; if it rejects a file, label-rename and growth attribution
  are skipped with a note and the rest of the report is unchanged.
- `--queries FILE` (repeatable), by extension:
  - `.json`: a Grafana dashboard export. Every `expr` string is a query, named after the
    nearest panel `title`.
  - `.yml` / `.yaml`: a Prometheus rule file (`groups[].rules[]`), or a Kubernetes
    `PrometheusRule` (`spec.groups`). Queries are named after `alert` or `record`.
  - anything else: one PromQL expression per line, optionally `name: expr`; blank lines and
    lines starting with `#` are skipped.

Query matching is by name, not a PromQL evaluation: a query reads a family when one of the
family's sample names (`x`, `x_bucket`, `x_sum`, `x_count`) appears in it outside string
literals, range selectors and comments, and it uses a label when the label name appears.

## What it reports

| Finding | From | Severity | Hits a query when |
| --- | --- | --- | --- |
| Renamed by the OTLP-to-Prometheus naming rules | [`prom-names`](prom-names.md) `metric_name` | error | it reads the old name |
| Label renamed | `promdiff` schema diff, `cardinality` label values | error | it reads the family and uses the old label |
| Family removed | `promdiff` `family_removed` | policy (fail) | it reads the family |
| Type changed | `promdiff` `type_changed` | policy (fail) | it reads the family |
| Label set changed | `promdiff` `label_schema_changed` | policy (fail) | it uses a removed label |
| Series growth over budget | `promdiff` `series_growth`, `cardinality` | policy (fail) | never (it is a cost and limits finding) |
| Other changes: families added, HELP changed, series added or gone | `promdiff` | policy (warn) | never |

**Renames.** A family that disappears and a family of the same type that appears are
reported as one rename when the `prom-names` naming function maps one name onto the other:
an OTLP unit suffix (`s` → `_seconds`, `ms` → `_milliseconds`, `By` → `_bytes`, `1` →
`_ratio`), the counter `_total` suffix, the exact milliseconds-to-seconds renaming of
`--convert-unit ms:s` (flagged as rescaled: thresholds need a factor of 0.001 as well), or
the reverse (suffixes turned off). Only one-to-one matches count. **Label renames** need one
label out, one label in, in the same family, with identical observed value sets.

The renames are projected before the comparison, exactly like `promdiff --rename-map`, so the
other findings describe what changed besides the names, and a renamed family is not also
reported as removed plus added.

**Severity** of the `promdiff` findings follows its policy (`--config`, same JSON as
[`promdiff --config`](promdiff.md#policy-and-growth-budget)); `fail` is reported as `error`, `warn` as `warning`.
`--max-growth PERCENT` overrides the growth budget (default 20).

**Growth attribution.** For the (up to three) families that grew most, `preflight` names the
label whose distinct-value count grew most, then runs `cardinality`'s label-removal check on
the after capture: if dropping the label with `metric_relabel_configs` would merge series,
the report says so and tells you to aggregate it at the source instead; if it is
collision-free, the follow-up command includes `--emit-config` to print the relabel rule.

## Follow-up commands

Every finding carries the command to run next:

| Finding | Next |
| --- | --- |
| renames | `otelwrench preflight B A --emit rename-map > renames.json`, then `otelwrench promdiff B A --rename-map renames.json` |
| removed, type changed, label set changed | `otelwrench preflight B A --emit allowlist > allowlist.json`, then `otelwrench promdiff B A [--rename-map renames.json] --allowlist allowlist.json` |
| series growth | `otelwrench cardinality A --drop-labels LABEL --metric NAME [...] [--emit-config]` |
| other changes | `otelwrench promdiff B A [--rename-map renames.json]` for the full list |

`--emit rename-map` prints the inferred renames as a [`promdiff --rename-map`](promdiff.md#explicit-rename-declarations)
file. `--emit allowlist` prints a `promdiff --allowlist` that accepts every change that is
left after the renames; delete the entries you do not accept. Both print JSON to stdout,
exit 0, and replace the report. Once both files are committed, `otelwrench promdiff` with
them is a CI gate that passes until something else changes.

## Output

Text by default (colour on a terminal; `--no-color` or `NO_COLOR` turns it off).
`--format json` prints an `otelwrench.preflight/v1` document:

```text
schema, otelwrench_version, before, after
summary     series_before, series_after, growth_percent, growth_budget_percent,
            families_before, families_after, errors, warnings, findings,
            queries, queries_hit, queries_by_kind, fail_on, failed, promdiff_status
findings[]  id, kind, severity, source, family, title, detail, fix, next[], queries[],
            plus kind-specific fields (renamed_to, rescaled, otlp_unit, strategy, steps;
            old_label, new_label; before, after; removed_labels, added_labels; drivers[])
queries[]   name, kind (panel, alert, record, query), source, expr, hit, findings[] (ids)
rename_map  the --emit rename-map document
allowlist   the --emit allowlist document
next        rename_map[], accept[]
notes[]     skipped checks
```

## Limits

- Two captures show what the exporter served at two moments. A family that is only emitted
  after some traffic, or a label value that appears later, is not in the capture.
- Name matching can flag a query that would still return data (for example through
  `or vector(0)`), and cannot see names built dynamically (Grafana template variables in a
  metric name).
- Rename detection covers the OTLP-to-Prometheus suffix rules and same-valued label renames.
  Other renames (for example a semantic-convention rename of a metric) are reported as a
  removal plus an added family; declare them with a hand-written `promdiff --rename-map`.

## Library use

```python
from otelwrench.preflight.core import build, load_side
from otelwrench.preflight.queries import load_queries
from otelwrench.promdiff.config import Config

doc = build(load_side("before.prom"), load_side("after.prom"),
            load_queries("alerts.yaml"), Config(), fail_on="error")
```
