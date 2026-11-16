# otelwrench loki

Preview where captured OTLP log attributes would go under two explicit Loki mapping profiles, entirely offline. The CLI explains each record's index labels, structured metadata and dropped keys, and compares the **observed distinct label sets** in the supplied file. No Loki deployment or endpoint is needed.

Original Apache-2.0 Python implementation. It does not copy Loki source, send logs, run configuration scripts, or edit production configuration. Loki already provides native OTLP ingestion; this tool is a limited local inspection aid, not a replacement or a claim that historical issues remain reproducible.

## When to use it

Use `otelwrench loki` before changing Loki's `limits_config.otlp_config` (or moving from defaults to an explicit profile) to see, on a real saved OTLP log export, which attributes become index labels, which become structured metadata, which are dropped, and how many streams the change creates or merges.

## Quick start

`otelwrench loki` needs PyYAML and google-re2, both installed with otelwrench (`pip install otelwrench`). The commands below use the samples in [`examples/loki/`](../examples/loki/), so run them from a clone of the repository.

```sh
otelwrench loki preview examples/loki/logs.json --config examples/loki/loki.yaml
otelwrench loki compare examples/loki/logs.json --before examples/loki/defaults.yaml --after examples/loki/loki.yaml
otelwrench loki explain examples/loki/explain/logs.json --config examples/loki/explain/profile.yaml \
  --record-id 'resourceLogs[0].scopeLogs[0].logRecords[0]' --key service_name
```

Runtime is offline. All three commands (`preview`, `compare`, `explain`) write one JSON report to stdout. Exit codes: `0` success, `2` invalid/unsupported input or filesystem error (message on stderr). `--help` and `--version` (`otelwrench loki 0.1.0`) are available. Config paths are required; a typo never silently selects defaults.

For a local push-format artifact, choose a **new** file in an existing directory:

```sh
mkdir -p .out
otelwrench loki preview examples/loki/logs.json --config examples/loki/loki.yaml --push-json .out/push.json
python -c 'import json; from pathlib import Path; assert json.loads(Path(".out/push.json").read_text()) == json.loads(Path("examples/loki/expected-push.json").read_text()); print("golden push matches")'
```

`--push-json` never makes a request. It validates all records first, writes a temporary file beside the destination, then atomically creates the destination without replacement. Existing files and symlinks fail; no directories are created automatically. Default preview/compare only read input files. Do not use an existing output path when repeating the example. Reports include dropped values: this tool is **not a redactor**.

## Reproducible synthetic example

`examples/loki/logs.json` is an OTLP `resourceLogs` request containing two records from service `checkout`, pod `p1` and pod `p2`. Each has scope information, `order.id`, and a deliberately synthetic `debug.secret` value.

`examples/loki/defaults.yaml` uses the bundled defaults. `examples/loki/loki.yaml` sets `ignore_defaults: true`, indexes only `service.name`, and drops `debug.secret`. Actual CLI and E2E results:

| Profile | Records | Observed streams | Dropped leaf occurrences |
| --- | ---: | ---: | ---: |
| Bundled defaults | 2 | 2 | 0 |
| Only `service.name` indexed | 2 | 1 | 2 |

Compare reports four changed placements: `k8s.pod.name` moves from index label to metadata twice, and `debug.secret` moves from metadata to drop twice. This is a finite fixture result, not a production cardinality estimate, cost prediction or performance claim.

The first projected entry is:

```json
{
  "stream": {"service_name": "checkout"},
  "value": ["1700000000000000001", "order 1 accepted", {
    "component": "payment", "k8s_pod_name": "p1", "order_id": "42",
    "scope_name": "example.checkout", "scope_version": "1.0"
  }]
}
```

Actual push output groups entries into `{"streams":[{"stream":{...},"values":[[timestamp,line,metadata], ...]}]}`. `examples/loki/expected-push.json` is the independently authored golden. Nanoseconds stay strings throughout; push values are stably sorted by integer timestamp within each stream. Records with equal timestamps are retained, without deduplication.

## Complete before/after record audit

Use `compare --include-records` to inspect all placements in one report, including unchanged attributes and records. Both comparison modes use the same capture and the same validation rules as `preview`.

```sh
otelwrench loki compare examples/loki/policy-audit/logs.json \
  --before examples/loki/policy-audit/before.yaml --after examples/loki/policy-audit/after.yaml \
  --include-records
```

This local synthetic OTLP fixture contains three records on pods `p1`, `p2`, `p1`. Both policies explicitly disable bundled defaults. Before indexes `service.name` and `k8s.pod.name`; after indexes `service.name`, moves pods to metadata, and drops scope `component` and log `debug.secret`. Expected **observed fixture counts only**: 3 records, 2 → 1 distinct label sets (`streamCountDelta: -1`), 9 changed placements and 0 → 6 dropped leaf occurrences. These counts imply no production cardinality, billing or performance result. `countScope: "observed_input_records_only"` labels every comparison report's counts.

The additional `records` array contains `{recordId, before, after}` entries in input order. Each side is a complete `preview` record with timestamp, line, labels, metadata, placements, dropped values and transformations. For the first record, both sides retain `service_name: "checkout"` as an index label and `order_id: "42"` as metadata. `k8s_pod_name: "p1"` moves from label to metadata; `component: "payment"` and `debug_secret: "SYNTHETIC-ONLY"` move from metadata to drop. Scope name/version remain metadata. Identity comparisons still include every record when requested. Detailed reports can be large and expose retained and dropped values; they are not redacted.

The companion `examples/loki/policy-audit/collisions.json` contains resource `shared.key`, scope `shared_key` and log `shared-key`, all normalizing to `shared_key`. The before policy indexes the resource key, while the after policy drops all three. Replacing `logs.json` with `collisions.json` in the command must exit `2`, emit no JSON to stdout, and identify `before: invalid:` and `after: invalid:` with the same normalized-key collision. No placement is invented for an ambiguous record. Tests also check each pair of resource/scope/log collisions independently.

After both profile files load successfully, `compare` evaluates both projections even if one fails. A semantic failure reports the first error for each policy (or marks a successful side `valid`) and returns no partial comparison or counts. Profile-loading and JSON-reading failures still stop before projection. An empty resource label can fail an indexing policy while a demoting policy succeeds; this is a policy-dependent value constraint, not an exception to collision validation.

## Observed stream merges and splits

Every successful comparison includes `streamTopology`, even without `--include-records`. It describes how the **same observed records** regroup between the two profiles:

- `before` and `after` list complete normalized label sets as `stream`, with `recordIds` and `recordCount`. Arrays sort lexically by label key/value pairs, exactly like preview. Record IDs retain input order within each stream.
- `transitions` list each nonempty before/after intersection: zero-based `beforeStreamIndex` and `afterStreamIndex` refer to those arrays; `recordIds` and `recordCount` identify the records moving along that intersection. Transitions sort by the index pair; records retain input order. Each input record occurs exactly once on each side and once in the transitions, even when timestamps and lines are identical.
- `merges` list every after stream receiving records from multiple before streams, using `afterStreamIndex` and sorted `beforeStreamIndexes`. `splits` list every before stream sending records to multiple after streams, using `beforeStreamIndex` and sorted `afterStreamIndexes`. Events sort by their single stream index. One stream can participate in both a split and a merge; these are local fan-in/fan-out events, not counts of disjoint groups.
- `membershipChanged` is true exactly when any merge or split exists. A one-to-one label change is still visible in the stream arrays and existing `changedStreams`, but does not change membership. Identity and metadata/drop-only changes have no merges or splits. Reason-only changes are still excluded from `changedPlacementCount`.

```sh
otelwrench loki compare examples/loki/stream-topology/logs.json \
  --before examples/loki/stream-topology/before.yaml --after examples/loki/stream-topology/after.yaml
```

Concrete input/output contract: four synthetic records have resource pairs `(route.old, route.new)` of `(x,u)`, `(x,v)`, `(y,u)`, `(y,v)`, in that order. Before indexes only `route.old`; after indexes only `route.new`; both disable defaults. The records deliberately share a timestamp and log line. Before stream 0 is `{"route_old":"x"}` with resource record IDs 0,1; stream 1 is `{"route_old":"y"}` with IDs 2,3. After stream 0 is `{"route_new":"u"}` with IDs 0,2; stream 1 is `{"route_new":"v"}` with IDs 1,3. Full IDs are `resourceLogs[N].scopeLogs[0].logRecords[0]`.

Expected output: 4 records, 2 → 2 observed streams, `streamCountDelta: 0`, 8 changed placements, four single-record transitions, **two splits and two merges**, and `membershipChanged: true`. The exact topology is checked in `examples/loki/stream-topology/expected-topology.json`. Reversing profiles swaps sides and reverses transitions; comparing either profile to itself gives two one-to-one transitions and no events. A simple 2 → 1 regrouping gives one merge; its reverse gives one split. These results describe only the supplied finite capture.

Negative/boundary contract: an empty capture has empty topology arrays and `membershipChanged: false`. A no-label projection uses the label set `{}` as an ordinary observed group (and retains the existing non-exportable warning). Invalid profiles, collisions including dropped keys, empty indexed values or any invalid later record still exit `2` with empty stdout; no partial topology is emitted. Reports include label values and all record IDs by default and can grow with capture size; they are not redacted. Indices are local to one comparison, not persistent stream identifiers.

## Trace a selected record and key

`explain` traces selector matches, precedence, flattening/normalization and final placement for an exact record ID from preview. `--key` selects a **normalized output key**, including a dropped key; omit it to trace every placement in that record. Quote record IDs to protect brackets from shell expansion.

```sh
otelwrench loki explain examples/loki/explain/logs.json \
  --config examples/loki/explain/profile.yaml \
  --record-id 'resourceLogs[0].scopeLogs[0].logRecords[0]' --key service_name
```

This synthetic fixture has resource `service.name: checkout` and `k8s.pod.name: p1`, nested log `net.peer` with `port: 443` and `host: example.invalid`, two attributes selected for drop, an empty AnyValue and a fixed scope name. Expected `explanations[0]` excerpt (other fields omitted):

```json
{
  "sourceRef": {
    "recordId": "resourceLogs[0].scopeLogs[0].logRecords[0]",
    "source": "resource", "sourcePath": ["service.name"]
  },
  "normalization": {"joinedKey": "service.name", "normalizedKey": "service_name"},
  "selection": {
    "ruleRef": "/limits_config/otlp_config/resource_attributes/attributes_config/0",
    "placement": "structured_metadata"
  },
  "final": {"key": "service_name", "placement": "structured_metadata", "value": "checkout"}
}
```

The full entry also contains `selection.rules` with selectors/actions/booleans/statuses, default/fallback decisions and the unchanged preview placement in `final`. The three rule statuses are `selected`, `shadowed`, `not_matched`: the literal `service.name` rule wins over a later matching RE2 `^service[.]` drop rule and the bundled default label list. A literal `service_name` selector does not match the original dotted name. Later selectors are tested only to explain shadowing; their actions are never applied.

With `--key net_peer_port`, the source path is `["net.peer", "port"]`, the joined key is `net.peer_port`, and the normalized key is `net_peer_port`. RE2 log rule `/limits_config/otlp_config/log_attributes/0` selects original **top-level** `net.peer`, so both nested leaves inherit metadata placement despite the later literal drop rule. In the full record, only `k8s_pod_name: p1` remains indexed; metadata is exactly `service_name: checkout`, `net_peer_port: "443"`, `net_peer_host: example.invalid`, `empty: ""` and `scope_name: example.checkout`. `internal_component` and `debug_private_value` are dropped.

`record` is exactly the normal preview record. `--key` filters only `explanations`, so the report still includes other retained and dropped values. Source references combine record ID, source level and exact original path segments; they distinguish a literal underscore key from nested segments. Rule references are zero-based JSON pointers into the supplied config. Default references such as `defaults.json#/attributes/12` point into the bundled snapshot included in the report. Unmatched metadata and fixed fields use `builtin:structured_metadata` and `builtin:fixed_otlp_field`. Fixed fields bypass attribute selectors.

Explain validates the **entire capture** using preview before selecting anything. Unknown record IDs/normalized keys fail with exit `2` and empty stdout. Flattening collisions (even dropped keys) and empty indexed values produce the same deterministic rejection as preview, without partial traces. For example, adding log key `net_peer_port` to this fixture fails with both `log:['net.peer', 'port']` and `log:['net_peer_port']` source references; changing resource `k8s.pod.name` to an empty AnyValue fails with its source and winning default rule reference. Explicitly demoting or dropping an empty value remains valid. A record with no labels can be explained with the same warning as preview. Invalid mappings, including unsupported scope/log labels and invalid RE2, remain errors. Explain has no `--push-json`, endpoint or live push option.

## Compare selector traces for one record

Add `--record-id` to `compare` to include `selectedRecordTrace` with `recordId`, `beforeStreamIndex`, `afterStreamIndex`, and `before`/`after` reports **exactly equal to `explain`** for each profile and the same selection. The stream indexes refer to `streamTopology.before`/`after` groups containing that record. `--key` optionally selects a normalized output key, including a dropped key; omit it for all placements. Selection is independent of `--include-records` and leaves all existing comparison fields/counts unchanged. Without `--record-id`, the trace field is absent.

```sh
otelwrench loki compare examples/loki/explain/logs.json \
  --before examples/loki/explain/profile.yaml --after examples/loki/explain/after.yaml \
  --record-id 'resourceLogs[0].scopeLogs[0].logRecords[0]' --key service_name
```

Concrete input/output contract: the existing explain capture has `service.name: checkout`. Before selects literal resource rule 0 (`attributes: [service.name]`); after skips unrelated rule 0 and selects RE2 rule 1 (`regex: '^service[.]name$'`). Both place `service_name` in metadata and shadow a later matching drop rule. In `selectedRecordTrace.before.explanations[0].selection`, `ruleRef` ends in `/resource_attributes/attributes_config/0` and rule statuses are `selected, shadowed, not_matched`. After's pointer ends in `/1`, with statuses `not_matched, selected, shadowed, not_matched`. Both final values are `checkout` in `structured_metadata`. Counts remain 1 record, 1 → 1 observed streams, **0 changed placements**, empty `changedStreams`, and no merge/split events. Both stream indexes are 0 and identify `{"k8s_pod_name":"p1"}`. Winner/selector/reason differences are visible in the traces and are never counted as placement changes, including when the winning rule pointer stays the same but its selector changes.

For a changed-placement example, select `resourceLogs[2].scopeLogs[0].logRecords[0]` and `--key route_old` with the stream-topology command above. Its indexes are before 1 and after 0; the original top-level `route.old` changes from explicit `index_label` to unmatched `structured_metadata`. The comparison still reports 8 changed placements and the same two merges/two splits. Identity comparisons retain equal traces. Nested leaves, default/fallback decisions, fixed fields and drops have the same trace semantics as `explain`.

Negative contract: `--key` requires `--record-id`; unknown IDs (including in empty captures), unknown normalized keys and original dotted key spellings fail with exit `2` and empty stdout. Both full projections validate **before record/key selection**: an invalid later record, a normalized collision even on dropped keys, an empty indexed value or invalid profile produces no partial comparison/trace. Semantic failures retain paired before/after diagnostics. `--key` filters explanations and decisions, not the complete selected records or global counts/topology. These reports expose retained/dropped values and may be large; they are not redacted. No files or requests are created by compare.

## Selected-record decision summary

Each selected comparison also includes `selectedRecordTrace.decisions`, one entry per selected placement, including unchanged decisions, in the before record's placement order. `--key` filters these entries and both sides' explanations together. No record selection means no trace or decision summary. The before/after explain reports and global counts/topology retain their existing meaning.

Each entry has `sourceRef` (record ID, source level and exact original path segments), `key` (normalized output key), `before` and `after` winning decisions, booleans `placementChanged` and `winnerChanged`, and a `changedFields` array. A winning decision is exactly `{ruleRef, placement, selector}`; `selector` is the winning explicit rule's original `attributes` list or `regex`, or `null` for defaults, fallback and fixed fields. Entries align by source, original path and normalized key, never by displayed value or rule pointer.

`changedFields` lists exactly the unequal before/after winning-decision fields in
fixed order `ruleRef`, `placement`, `selector`, with no duplicates. The literal-to-RE2
`service_name` example reports `["ruleRef", "selector"]`; `selector-only.yaml`
reports `["selector"]` at the same pointer. An action change at the same pointer
and selector reports `["placement"]`; a pointer move alone reports `["ruleRef"]`.
Identity, shadowed-only edits and unchanged default/fallback/fixed winners report
`[]`. `winnerChanged` equals `bool(changedFields)` and `placementChanged` equals
membership of `"placement"`. Attribute-list order and exact regex text count as
selector differences, even when both select the same keys.

For a mixed same-pointer service-selector edit and a `net.peer` regex action change,
`service_name` reports `["selector"]`, then nested `net_peer_port` and
`net_peer_host` each report `["placement"]` in before-placement order. Replacing
that regex with an attributes selector as well gives `["placement", "selector"]`
for each nested leaf. In topology record 2, both `route_old` and `route_new` report
`["ruleRef", "placement", "selector"]` in that order. Default-to-fallback action
changes report `["ruleRef", "placement"]` because both selectors are `null`.

This field is local to each selected decision in all seven views, after optional
normalized key selection. It adds no option, trace without record selection, or
global total. Filters, pre-filter counts, source paths, exact raw explain sides,
complete retained/dropped values, stream indexes and all global fields remain
visible as before. It does not redact values or bypass whole-capture validation;
unknown selections, filters without record selection, filter conflicts and invalid
captures or profiles still exit `2` with empty stdout.

`placementChanged` compares placement actions only. `winnerChanged` compares the complete winning decision: pointer, action and selector. A pointer move, action change or selector edit at the same pointer sets it; edits only to shadowed/unmatched rules do not. Selectors compare literally, including attribute-list order and regex text; no regex or selector semantic equivalence is inferred. The summary does not add a global change count or affect `changedPlacementCount`/merge/split events.

Concrete contracts using the compare example above:

- For `service_name`, before is `{"ruleRef":"/limits_config/otlp_config/resource_attributes/attributes_config/0","placement":"structured_metadata","selector":{"attributes":["service.name"]}}`; after has pointer `/limits_config/otlp_config/resource_attributes/attributes_config/1`, the same placement, and `{"regex":"^service[.]name$"}`. `placementChanged` is false and `winnerChanged` is true. The exact entry is in `examples/loki/explain/expected-decisions.json`.
- Replacing the after profile with `examples/loki/explain/selector-only.yaml` changes only rule 0's literal selector to the equivalent RE2 selector. Both pointers stay at `/0`; flags are still false/true. Both examples have zero global placement changes and no merge/split events.
- The stream-topology example for record 2 and `route_old` has flags true/true, before explicit rule 0 indexing `route.old`, after `builtin:structured_metadata` with a null selector, and indexes 1 → 0. Global totals remain 8 changed placements, two merges and two splits.
- Identity has both flags false for every selected entry. Default `k8s_pod_name`, fallback `empty`, fixed `scope_name`, dropped keys and nested `net_peer` leaves retain exact source references. An empty-placement record has `decisions: []`.

Negative contract: selection errors, invalid later records, dropped-key collisions, empty indexed values and invalid profiles still exit `2` with empty stdout and no partial summary. Whole-capture paired validation precedes selection. Summaries repeat selectors and references, not values, but the complete selected records and all retained/dropped values remain present even with `--key`; reports are not redacted. Output grows with selected leaves and selector size.

Use `compare --changed-decisions-only` with `--record-id` to keep only entries whose `winnerChanged` is true in `selectedRecordTrace.decisions`. The entry schema and before-placement order stay the same. Without this flag, all selected decisions remain present. With `--key`, the normalized key must exist first, then the change filter applies: a known unchanged key succeeds with `decisions: []`, while an unknown key still fails.

```sh
otelwrench loki compare examples/loki/explain/logs.json \
  --before examples/loki/explain/profile.yaml --after examples/loki/explain/after.yaml \
  --record-id 'resourceLogs[0].scopeLogs[0].logRecords[0]' --changed-decisions-only
```

This emits exactly the one `service_name` decision in `examples/loki/explain/expected-decisions.json` (false `placementChanged`, true `winnerChanged`); both raw sides still have all eight explanations. Using `selector-only.yaml` instead also keeps that decision with both rule pointers at `/0`. Identity or shadowed-rule-only edits yield `decisions: []`. In the stream-topology example for record 2, the filtered keys are `route_old`, `route_new` in that order, both with true/true flags; `--key route_old` keeps just the first. Global totals stay at eight placement changes, two merges and two splits, and selected stream indexes stay 1 → 0.

The flag requires `--record-id`, even with `--include-records`; missing selection exits `2` with empty stdout. Unknown selections, invalid full captures and invalid profiles retain the same errors and no partial output. Filtering changes only the summary array and its `decisionFilter` label: raw before/after explain reports, complete selected records (including retained/dropped values), optional all-record audit, stream indexes and global counts/topology remain intact. It does not redact values or avoid full-capture validation, and an empty summary does not mean the global report has no changes.

Each selected trace also includes `decisionCounts`, exactly
`{"selectedLeafCount":8,"winnerChangedCount":1,"placementChangedCount":0,"reasonChangedCount":1}` for the
all-key literal-to-RE2 example above. These nonnegative integer totals describe
only the selected record and optional normalized `--key` / `--decision-source`, **before** any summary
filtering. The same totals are present in all seven decision views and are
absent together with the trace when no record is selected. `selectedLeafCount`
includes unchanged, dropped, nested and fixed-field leaves; it counts leaves,
not rules or distinct attribute names. `winnerChangedCount` uses the same literal
winning-decision comparison as the entries; `placementChangedCount` counts action
changes among those selected leaves. `reasonChangedCount` counts leaves with
`winnerChanged: true` and `placementChanged: false`, including literal selector
edits at the same rule pointer. It adds no global reason total.

Concrete local totals (selected / winner changed / placement changed / reason changed),
identical in all seven views:

- All-key literal-to-RE2 or same-pointer selector change: **8 / 1 / 0 / 1**.
  Winner-changed and reason-only arrays have one entry; placement-only is empty.
- Mixed same-pointer selector edit and two nested `net.peer` action changes:
  **8 / 3 / 2 / 1**. Reason-only retains just `service_name`.
- Identity or shadowed-only edits on that capture: **8 / 0 / 0 / 0**.
- `--key service_name` for the reason-only change: **1 / 1 / 0 / 1**; known unchanged
  dropped key `--key debug_private_value`: **1 / 0 / 0 / 0**, with its complete value
  still present in the raw selected record.
- Stream-topology record 2, all keys: **2 / 2 / 2 / 0**; `--key route_old`:
  **1 / 1 / 1 / 0**. Global `changedPlacementCount` stays **8**, with two merges and
  two splits. Selected totals never sum changes across other records.
- A valid timestamped record with no attributes or emitted fixed fields:
  **0 / 0 / 0 / 0**, an empty decisions array and the existing no-label warning.

Selection and whole-capture validation happen before these totals are emitted:
unknown records/keys (including an ID in an empty capture), invalid later records,
collisions even on dropped keys, empty indexed values and invalid profiles still
exit `2` with empty stdout and no partial counts. Both raw explain-equivalent
sides, stream indexes, optional complete record audit and all global fields keep
their existing semantics. Counts and filtering do not redact full selected
records or retained/dropped values. An empty local summary or zero local change
totals do not imply that the capture has no changes elsewhere.

Each selected trace additionally includes `decisionFieldCounts`, exactly
`{"ruleRef":1,"placement":0,"selector":1}` for the literal-to-RE2 example.
These three nonnegative integers count selected leaves whose `changedFields`
contains that field, after optional normalized `--key` selection and **before**
any of the seven summary views. A leaf can contribute to several fields, so the
three totals need not sum to `winnerChangedCount`; `placement` always equals
`decisionCounts.placementChangedCount`. No record selection means no trace or
field counters. These counters add no global field-change total.

Concrete field totals (`ruleRef` / `placement` / `selector`), identical in all views:

- Literal-to-RE2: **1 / 0 / 1**; same-pointer selector-only: **0 / 0 / 1**.
- Same-pointer action-only: **0 / 1 / 0**; pointer-only: **1 / 0 / 0**.
- Mixed same-regex nested action changes plus service selector edit: **0 / 2 / 1**.
  Replacing the nested regex with literal `attributes: [net.peer]` while changing
  its action instead gives **0 / 2 / 3**: both inherited nested leaves count.
- Stream-topology record 2: **2 / 2 / 2**; `--key route_old`: **1 / 1 / 1**,
  while global placement changes remain **8** and selected indexes remain 1 → 0.
- Identity, shadowed-only edits, unchanged selected keys and zero-leaf records:
  **0 / 0 / 0**. Defaults/fallback/fixed winners compare literally too.

Field totals describe only selected winning decisions, never shadowed matches,
semantic selector equivalence or other records. They preserve `decisionCounts`,
exact decisions, raw before/after sides, complete record audit, indexes and all
global fields. Empty filtered arrays can coexist with positive field totals and
full retained/dropped values: this is not redaction. The existing negative
contract still applies: unknown/original-spelling selections, invalid full
captures/profiles, missing required selection and filter conflicts exit `2`
with empty stdout and no partial trace or counters.

Every selected trace also includes `decisionCountsBySource`, with exactly four
buckets in `resource`, `scope`, `log`, `field` order. Each bucket has the same
four nonnegative integer counters as `decisionCounts`, in the same order:
`selectedLeafCount`, `winnerChangedCount`, `placementChangedCount`,
`reasonChangedCount`. A leaf belongs to its `sourceRef.source` bucket; `field`
means emitted fixed fields, not an attribute named “field”. Zero buckets are
always included. Each counter summed across buckets equals `decisionCounts`.

Concrete input/output contract for the existing all-key literal-to-RE2 capture
(selected / winner / placement / reason):

```json
{
  "resource": {"selectedLeafCount":2,"winnerChangedCount":1,"placementChangedCount":0,"reasonChangedCount":1},
  "scope": {"selectedLeafCount":1,"winnerChangedCount":0,"placementChangedCount":0,"reasonChangedCount":0},
  "log": {"selectedLeafCount":4,"winnerChangedCount":0,"placementChangedCount":0,"reasonChangedCount":0},
  "field": {"selectedLeafCount":1,"winnerChangedCount":0,"placementChangedCount":0,"reasonChangedCount":0}
}
```

Mixed same-pointer service-selector and nested `net.peer` action edits keep
resource **2 / 1 / 0 / 1**, change log to **4 / 2 / 2 / 0**, and keep scope/field
unchanged. Topology record 2 gives resource **2 / 2 / 2 / 0** and three zero
buckets. Identity/shadowed-only winners keep the selected counts with all
change counters zero; a zero-leaf record has four entirely zero buckets.

Counts apply after record/normalized-key (and optional source) selection and **before all seven
summary views**. A selected key populates only its source bucket, including
dropped `debug_private_value`, inherited nested `net_peer_port`, and fixed
`scope_name`. Empty filtered decisions can coexist with positive source counts.
The two existing counter objects, exact decisions, raw explain-equivalent sides,
full retained/dropped values, indexes, optional audits and globals are preserved.
This local breakdown adds no CLI flag, global totals or redaction; it does not
measure other records, shadowed edits or semantic selector equivalence.

Negative contract: no `--record-id` emits no trace or breakdown. Missing required
selection, conflicting filters, unknown records/normalized keys (including
original dotted spellings), invalid full captures and invalid profiles still
exit `2` with empty stdout. Full paired validation precedes counting; source
buckets cannot expose a partial result or bypass validation. Complete selected
record values remain visible even with key selection or an empty summary.

Optional source selection narrows the local summary to one mapping layer:

```bash
otelwrench loki compare examples/loki/explain/logs.json \
  --before examples/loki/explain/profile.yaml --after examples/loki/explain/after.yaml \
  --record-id 'resourceLogs[0].scopeLogs[0].logRecords[0]' \
  --decision-source resource --placement-decisions-only --include-records
```

`--decision-source resource|scope|log|field` (API: `decision_source=...`, default
`None`) requires `--record-id`. After both full projections and record/normalized
key validation, source selection keeps exactly decisions with that
`sourceRef.source`, in their original order. All three local counter objects
(`decisionCounts`, `decisionFieldCounts`, `decisionCountsBySource`) describe this
record/key/source selection **before** any of the seven views. Nonselected source
buckets remain present with all four counters zero. Each source combines with
any one view; `field` means fixed OTLP fields, including scope name.

Only an explicitly source-selected trace adds `decisionSource`, exactly the
requested string, including empty summaries. Omission (or API `None`) preserves
every existing output field/value and emits no `decisionSource` label. The view
label `decisionFilter` is independent. In the command above, `decisionSource` is
`"resource"`, `decisionFilter` is `"placement_changed"`, decisions are empty,
`decisionCounts` is **2 / 1 / 0 / 1**, `decisionFieldCounts` is
`{"ruleRef":1,"placement":0,"selector":1}`, and only the resource source bucket
is nonzero. With `--key service_name --decision-source log`, every view succeeds
with empty decisions and all three counter objects zero (four zero source buckets).
The known key is validated before source selection; unknown keys, including
`service.name`, still fail instead of becoming empty successes.

Negative contract: missing record selection (even with `--include-records`) or
an invalid source (including `all`, empty or uppercase spellings) exits `2` with
empty stdout. API invalid non-`None` values raise `PreviewError`. Conflicting
views, unknown records/keys, malformed JSON, invalid profiles and invalid records
elsewhere in the full capture remain atomic errors. Source selection changes
only the local decisions, three counter objects and explicit source label. Raw
explain-equivalent sides/explanations, complete selected records including all
retained/dropped values, stream indexes, optional all-record audits and global
fields remain intact. This is not redaction, global counting, or a way to bypass
full-capture validation; empty summaries can coexist with global changes.

For action changes alone, use `--placement-decisions-only` with `--record-id`.
It keeps only `placementChanged: true` entries in `selectedRecordTrace.decisions`,
in the same before-placement order and with the same entry schema:

```sh
otelwrench loki compare examples/loki/stream-topology/logs.json \
  --before examples/loki/stream-topology/before.yaml --after examples/loki/stream-topology/after.yaml \
  --record-id 'resourceLogs[2].scopeLogs[0].logRecords[0]' --placement-decisions-only
```

This keeps `route_old`, `route_new`, both with true placement/winner flags, and
local counts **2 / 2 / 2 / 0**. Global totals remain eight placement changes, two
merges and two splits; selected indexes remain 1 → 0. For the literal-to-RE2
or same-pointer selector-only example, this view emits `decisions: []` with
counts **8 / 1 / 0 / 1**. Identity and shadowed-only edits give **8 / 0 / 0 / 0** and
no decisions; a zero-leaf record gives **0 / 0 / 0 / 0**. If a profile both changes
the `service.name` selector and drops the two `net.peer` leaves, counts are
**8 / 3 / 2 / 1** and only `net_peer_port`, `net_peer_host` remain in this view.
With `--key`, selection still precedes filtering: known unchanged dropped key
`debug_private_value` succeeds with **1 / 0 / 0 / 0** and no decisions; unknown keys
still fail.

The six summary filters `--changed-decisions-only`,
`--placement-decisions-only`, `--reason-decisions-only`,
`--selector-decisions-only`, `--rule-ref-decisions-only` and
`--unchanged-decisions-only` are mutually exclusive.
Combining two or more, or using one without `--record-id`, exits `2` with empty
stdout, including with `--include-records`. Invalid full captures/profiles and unknown selections still
fail without partial traces/counts. All filters preserve pre-filter
`decisionCounts`, raw explain-equivalent sides and complete selected records,
stream indexes, optional all-record audit and all global fields. They do not
redact retained/dropped values or count winning changes across all records.

For winning-rule changes that keep the same placement, use
`--reason-decisions-only` with `--record-id`. It retains exactly entries with
`winnerChanged: true` and `placementChanged: false`, in before-placement order,
and labels the view `decisionFilter: "reason_changed"` even when empty:

```sh
otelwrench loki compare examples/loki/explain/logs.json \
  --before examples/loki/explain/profile.yaml --after examples/loki/explain/after.yaml \
  --record-id 'resourceLogs[0].scopeLogs[0].logRecords[0]' --reason-decisions-only
```

This keeps exactly `service_name` from `examples/loki/explain/expected-decisions.json`
with local counts **8 / 1 / 0 / 1**. Using `selector-only.yaml` also retains that
entry, with both rule pointers ending in `/0`. In the mixed selector/action
example, only `service_name` survives with counts **8 / 3 / 2 / 1**. Topology record 2
has no reason-only decisions with **2 / 2 / 2 / 0**; indexes stay 1 → 0 and all eight
global placement changes, two merges and two splits remain visible. Identity
and shadowed-only edits give no decisions with **8 / 0 / 0 / 0**; a zero-leaf record
gives **0 / 0 / 0 / 0**, all explicitly labeled `reason_changed`.

Selection precedes filtering: `--key service_name` gives **1 / 1 / 0 / 1** and the
same reason-only entry; known unchanged `--key debug_private_value` succeeds
with **1 / 0 / 0 / 0** and no decisions. Its raw `SYNTHETIC-ONLY` dropped value and
complete selected record remain visible. Unknown selections, invalid full
captures/profiles, missing record selection and conflicting summary filters
exit `2` with empty stdout, including with `--include-records`. This view
preserves pre-filter local counts, exact raw sides, stream indexes, optional
all-record audit and every global field. It does not redact values or add
all-record winner totals; an empty local summary does not imply no global changes.

For literal winning-selector changes, use `--selector-decisions-only` with
`--record-id`. It retains exactly entries whose `changedFields` contains
`"selector"`, including entries that also change placement, in before-placement
order with the existing entry schema. The label is `decisionFilter:
"selector_changed"`, even when the array is empty:

```sh
otelwrench loki compare examples/loki/explain/logs.json \
  --before examples/loki/explain/profile.yaml --after examples/loki/explain/after.yaml \
  --record-id 'resourceLogs[0].scopeLogs[0].logRecords[0]' --selector-decisions-only
```

Concrete input/output contract:

- Literal-to-RE2 and same-pointer `selector-only.yaml` retain just `service_name`,
  with local decision counts **8 / 1 / 0 / 1** and field totals **1 / 0 / 1**
  or **0 / 0 / 1**, respectively. Selectors compare literally, including list
  order and regex text; this does not infer semantic equivalence.
- Pointer-only and action-only changes retain no decisions, even with positive
  `ruleRef` or `placement` field counts. Their raw winning decisions remain visible.
- A service selector edit plus same-regex nested action changes retains only
  `service_name`, with counts **8 / 3 / 2 / 1** and field totals **0 / 2 / 1**.
  Replacing the nested regex with `attributes: [net.peer]` as well retains
  `service_name`, `net_peer_port`, `net_peer_host` in that order; field totals
  are **0 / 2 / 3** and decision counts stay **8 / 3 / 2 / 1**.
- Topology record 2 retains `route_old`, `route_new`, with counts **2 / 2 / 2 / 0**
  and field totals **2 / 2 / 2**. `--key route_old` keeps just that entry and
  totals **1 / 1 / 1 / 0** and **1 / 1 / 1**. Indexes stay 1 → 0; all eight global
  placement changes and both merges/splits remain visible.
- Identity, shadowed-only edits and zero-leaf records succeed with empty arrays.
  Known unchanged `--key debug_private_value` succeeds with an empty array and
  counts **1 / 0 / 0 / 0**; its full record and `SYNTHETIC-ONLY` dropped value remain.
  Default-to-fallback changes with null selectors are excluded; replacing null
  with an explicit selector is included. Fixed fields keep null selectors.

Normalized key selection happens before filtering, and both local counter
objects describe that selection before filtering. This view changes only
`decisions` and `decisionFilter`; complete raw sides, retained/dropped values,
stream indexes, optional all-record audit and all global fields remain intact.
It adds no global counters or implicit all-record tracing and does not redact.
Negative contract: this flag requires `--record-id` and cannot combine with
any other summary filter, including with `--include-records`. Missing selection,
conflicts, unknown/original-spelling selections, invalid full captures (including
later records, dropped-key collisions and empty indexed values) and invalid
profiles exit `2` with empty stdout and no partial report. Whole-capture paired
validation still precedes selection. Empty local summaries do not imply no
global changes.

For literal winning rule-reference changes, use `--rule-ref-decisions-only`
with `--record-id`. It keeps exactly decisions whose `changedFields` contains
`"ruleRef"`, including changes that also alter placement or selectors, in
before-placement order. `decisionFilter` is `"rule_ref_changed"` even for an
empty array:

```sh
otelwrench loki compare examples/loki/explain/logs.json \
  --before examples/loki/explain/profile.yaml --after examples/loki/explain/after.yaml \
  --record-id 'resourceLogs[0].scopeLogs[0].logRecords[0]' --rule-ref-decisions-only
```

Concrete contracts using the existing comparison fixtures:

- Literal-to-RE2 and pointer-only relocation retain exactly `service_name`.
  The example above preserves the golden decision unchanged, local decision
  counts **8 / 1 / 0 / 1** and field counts **1 / 0 / 1**.
- Same-pointer selector-only, action-only, and both mixed nested cases retain
  no decisions, despite positive selector or placement counts. A changed
  action or selector at the same literal pointer is excluded.
- Defaults-to-fallback plus nested drop transitions retain `service_name`,
  `k8s_pod_name`, `net_peer_port`, `net_peer_host` in that order, with decision
  counts **8 / 4 / 4 / 0** and field counts **4 / 4 / 2**. Default-to-explicit
  retains only `service_name`.
- Topology record 2 retains `route_old`, `route_new`; decision counts remain
  **2 / 2 / 2 / 0**, field counts **2 / 2 / 2**, indexes **1 → 0** and global
  placement changes **8**. Normalized `--key route_new` selects before filtering,
  retaining just that key and local counts **1 / 1 / 1 / 0**, **1 / 1 / 1**.
- Identity, shadowed-only edits, known unchanged selected keys and zero-leaf
  records succeed with `decisions: []` and `decisionFilter: "rule_ref_changed"`.
  Both local counter objects remain the exact pre-filter totals.

This filter changes only `decisions` and `decisionFilter`. Exact raw decisions,
full before/after explanations, retained/dropped values (including
`SYNTHETIC-ONLY`), indexes, optional audits and globals remain visible. It adds
no global totals or implicit all-record tracing, and is not redaction.
It requires `--record-id` and conflicts with each other summary filter and all
combinations of them. Missing selection, unknown record/normalized key,
invalid full captures (including unselected records) and invalid profiles
exit `2` with empty stdout. Both full projections must validate before
record/key selection and filtering; no partial reports are emitted.

For unchanged literal winning decisions, use `--unchanged-decisions-only`
with `--record-id`. It retains exactly entries with `changedFields: []`, in
before-placement order, and labels `decisionFilter: "unchanged"` even when empty.
Record/normalized-key selection precedes filtering.

```sh
otelwrench loki compare examples/loki/explain/logs.json \
  --before examples/loki/explain/profile.yaml --after examples/loki/explain/after.yaml \
  --record-id 'resourceLogs[0].scopeLogs[0].logRecords[0]' --unchanged-decisions-only
```

Concrete contracts using the existing fixtures:

- Identity and shadowed-only edits retain all eight selected leaves. Literal-to-RE2,
  pointer-only, same-pointer selector-only, action-only and default-to-explicit
  exclude `service_name`, retaining the other seven leaves: `k8s_pod_name`,
  `internal_component`, `net_peer_port`, `net_peer_host`, `debug_private_value`,
  `empty`, `scope_name`, in that order.
- Both mixed nested selector/action cases additionally exclude `net_peer_port`
  and `net_peer_host`, retaining five leaves. Defaults/fallback plus nested drop
  transitions retain `internal_component`, `debug_private_value`, `empty`, `scope_name`.
- Topology record 2 retains no decisions despite counts **2 / 2 / 2 / 0** and
  field counts **2 / 2 / 2**. Zero-leaf records also succeed empty with the
  `unchanged` label and zero counters. Known unchanged normalized keys, including
  dropped `debug_private_value`, retain one entry; known changed keys succeed empty.
- The literal-to-RE2 example retains seven decisions with pre-filter local counts
  **8 / 1 / 0 / 1** and field counts **1 / 0 / 1**. The unchanged and winner-changed
  views partition the selected leaves exactly.

Only `decisions` and `decisionFilter` change. Both counter objects stay pre-filter;
exact retained decision entries, complete raw sides/full values (including dropped
`SYNTHETIC-ONLY`), indexes, optional audits and all globals stay intact. Unchanged
means literal winning pointer/action/selector equality; shadowed-only edits do not
change winners. This adds no global totals, redaction or implicit all-record trace.
The flag requires `--record-id` and conflicts with every other summary filter and
all combinations of them. Missing selection, unknown records/normalized keys,
invalid full captures (including unselected later records, dropped-key collisions
and empty indexed values) or invalid profiles exit `2` with empty stdout.
Both complete projections must validate before selection and filtering.

Every selected trace labels the requested view with `decisionFilter`, including
when filtering leaves no decisions or all entries survive. This additive string
field lets a saved report identify the view without the original CLI command:

| Compare options (with `--record-id`) | `selectedRecordTrace.decisionFilter` | Decisions included |
| --- | --- | --- |
| No summary filter | `"all"` | All selected leaves |
| `--changed-decisions-only` | `"winner_changed"` | `winnerChanged: true` |
| `--placement-decisions-only` | `"placement_changed"` | `placementChanged: true` |
| `--reason-decisions-only` | `"reason_changed"` | `winnerChanged: true` and `placementChanged: false` |
| `--selector-decisions-only` | `"selector_changed"` | `changedFields` contains `"selector"` |
| `--rule-ref-decisions-only` | `"rule_ref_changed"` | `changedFields` contains `"ruleRef"` |
| `--unchanged-decisions-only` | `"unchanged"` | `changedFields: []` |

For the all-key literal-to-RE2 change, these seven views contain 8, 1, 0, 1, 1,
1 and 7 decisions respectively. The same-pointer selector change has 0 in the
rule-reference view; the other lengths match. Both have local counts
**8 / 1 / 0 / 1** in every view.
Identity and shadowed-only edits have 8, 0, 0, 0, 0, 0 and 8 decisions with **8 / 0 / 0 / 0**
counts. A valid zero-leaf record has `decisions: []` and **0 / 0 / 0 / 0** counts in
all seven views, but the labels still differ. Known unchanged
`--key debug_private_value` has 1, 0, 0, 0, 0, 0 and 1 decisions with **1 / 0 / 0 / 0** counts;
its raw `SYNTHETIC-ONLY` value remains visible in every view.

In the mixed selector/action example above, decision lengths are 8, 3, 2, 1, 1, 0 and 5
with **8 / 3 / 2 / 1** counts: winner filtering keeps `service_name`, `net_peer_port`,
`net_peer_host`; placement filtering keeps only the latter two in that order;
reason and selector filtering each keep only `service_name` in this same-regex
action-change example.
Topology record 2 retains `route_old`, `route_new` and **2 / 2 / 2 / 0** counts in
the first three views, the selector view and the rule-reference view; reason
and unchanged filtering keep no decisions with the same counts.

`decisionFilter` describes only the selected summary, independent of `--key`
and `--include-records`. It does not filter the raw explanations or complete
selected records, change pre-filter counts or global fields, redact values, or
provide all-record winner totals. Without record selection, no trace or view
metadata is emitted. Missing/conflicting filter dependencies, unknown
selections and invalid full captures/profiles still exit `2` with empty stdout
and no partial trace, counts or view metadata.

## Supported config and rule order

Supply an extracted **mapping profile**, not a complete Loki server config. Only these YAML fields are accepted:

```yaml
limits_config:
  allow_structured_metadata: true   # optional; false is rejected
  otlp_config:
    resource_attributes:
      ignore_defaults: true        # optional, default false
      attributes_config:
        - action: index_label
          attributes: [service.name]
        - action: structured_metadata
          regex: '^k8s[.]'
        - action: drop
          regex: '^private[.]'
    scope_attributes:
      - action: drop
        attributes: [internal.component]
    log_attributes:
      - action: structured_metadata
        attributes: [order.id]
      - action: drop
        regex: '.*'
```

Rules run in file order, **first matching rule wins**. Explicit resource rules precede fallback to the bundled defaults. Unmatched attributes become structured metadata. `ignore_defaults: true` disables only the default resource-label list; explicit rules still run. Each rule must select exactly one nonempty unique `attributes` list or one nonempty `regex`. Rules match original, case-sensitive **top-level** attribute names before flattening/normalization; a selected map's leaves all inherit the same action.

Regex uses RE2 syntax and substring search; use `^...$` for full-key matching. Backreferences and look-around fail compilation. Resource actions: `index_label`, `structured_metadata`, `drop`. Scope/log actions: `structured_metadata`, `drop`; scope/log `index_label` is explicitly rejected, even if a newer Loki version supports log labels. Attribute rules do not affect fixed OTLP fields.

All unknown config fields are errors, including distributor overrides, runtime tenant overrides, service discovery, severity promotion, and ingestion limits. YAML duplicate keys, merge keys, anchors, aliases, extra documents, and arbitrary object tags are rejected. There is no environment interpolation or script execution.

## Input and value mapping contract

One UTF-8 JSON object with camelCase `resourceLogs` is supported (not NDJSON, binary protobuf, snake_case, a Collector YAML pipeline, or response/query JSON). Unknown fields and duplicate object keys fail. Optional protobuf collections/containers can be absent, but explicit `null` is unsupported. Empty `resourceLogs` is valid and produces zero records/streams.

Supported fields:

- ResourceLogs: `resource`, `scopeLogs`, `schemaUrl`; Resource: `attributes`, `droppedAttributesCount`.
- ScopeLogs: `scope`, `logRecords`, `schemaUrl`; InstrumentationScope: `name`, `version`, `attributes`, `droppedAttributesCount`.
- LogRecord: `timeUnixNano`, `observedTimeUnixNano`, `body`, `attributes`, `severityNumber`, `severityText`, `traceId`, `spanId`, `flags`, `droppedAttributesCount`, `eventName`.

Attribute entries require a nonempty string `key` and a `value` AnyValue. Keys must be unique within every attribute/map list. An AnyValue is `{}` (empty) or exactly one of:

| Variant | Accepted JSON value | Loki string representation |
| --- | --- | --- |
| `stringValue` | UTF-8 string | unchanged |
| `boolValue` | boolean | `true` / `false` |
| `intValue` | canonical decimal int64 **string** (no leading zeros or `-0`) | exact decimal digits |
| `doubleValue` | finite JSON number | binary64 decimal text; no NaN/Infinity |
| `bytesValue` | canonical padded base64 | same base64 string |
| `arrayValue` | object with optional `values` AnyValue array | compact JSON; native integers/booleans, nested maps/arrays, base64 strings for bytes, `null` for empty values |
| `kvlistValue` | object with optional `values` key/value array | attribute maps flatten; body/array maps become compact JSON |
| empty `{}` | no variant | empty string (or `null` inside JSON containers) |

Nested attribute maps join path segments using `_`. Replace every character outside `[A-Za-z0-9_]` with `_`; prefix `_` for a leading digit. No case folding or automatic collision renaming occurs. Example: `net.peer` → `{port: 443}` becomes `net_peer_port: "443"`. Arrays stay a single string leaf and are never flattened. Empty maps as an attribute leaf fail because flattening would lose them; empty maps in a body or array remain `{}`. Duplicate keys or normalized collisions fail even when values agree, including across resource/scope/log/fixed fields and attributes configured to drop. This conservative policy avoids ambiguous projections.

Missing body is the empty AnyValue and becomes an empty line. Non-string bodies use the same primitive/compact-JSON conversion. Map keys sort lexically; numbers use fixed decimal notation for `1e-6 <= abs(x) < 1e21` and exponent notation otherwise. This is deterministic local serialization, not a guarantee of byte-identical floating-point formatting across all Collector/Go versions.

Timestamps must be canonical nonnegative uint64 **decimal strings**. Choose nonzero `timeUnixNano`, otherwise nonzero `observedTimeUnixNano`; reject when neither is present/nonzero. No wall-clock ingestion time is invented. When both are set, observed time also becomes `observed_timestamp` metadata. Preview preserves the full uint64 range; push export rejects values above signed int64 nanoseconds.

Nonempty scope name/version become `scope_name`/`scope_version`. Nonzero scope dropped count becomes `scope_dropped_attributes_count`. Nonzero severity number (integer 0–24), flags (uint32), dropped count (uint32), nonempty severity text and event name become `severity_number`, `flags`, `dropped_attributes_count`, `severity_text`, `event_name`. Trace/span IDs must be nonzero 32/16 hex digits and become lowercase `trace_id`/`span_id`; base64 IDs are rejected. Zero/empty optional fixed fields are protobuf defaults and are not emitted. Unknown/new OTLP fields fail rather than disappear.

## Report, losses and compatibility limits

`preview` includes the explicit effective profile and default-list provenance, summary counts, stream membership by record ID, and per-record `stream`, `structuredMetadata`, `placements`, `dropped` and transformations. `sourcePath` retains unambiguous original path segments. Every flattened attribute leaf has a placement and reason. Drop counts count leaf **occurrences**, including resource/scope leaves replicated per record. `compare` includes both summaries, a signed stream-count delta, every changed placement and changed record label set, and the observed stream topology described above; unchanged placement with a different rule reason is not counted as a placement change. `--include-records` adds complete per-record before/after projections, including unchanged placements; omitting it excludes those full projections but still includes all stream memberships. These are additive fields in report format version 1.

Stringification discards AnyValue type; flattening discards map structure. Reports explicitly mark these transformations. Schema URLs are preserved in report diagnostics but not push metadata; resource dropped counts are likewise diagnostic-only. Scope/log dropped counts are metadata and diagnostics indicating data was already lost before capture. Fixed fields default to metadata and cannot be relabeled/dropped by attribute rules.

This previews only the documented attribute-mapping subset. It does not model service-name discovery, server defaults beyond the bundled resource list, resource `entityRefs`, processors, tenant overrides, label/line/metadata limits, timestamp age windows, out-of-order handling, storage schemas, authentication or live ingestion. An otherwise valid exported artifact may be rejected by a real deployment. No-label projections remain inspectable with a warning but cannot be exported. Empty index-label values are rejected; explicitly demote or drop them. Input caps are 32 MiB for JSON and 1 MiB for YAML, with AnyValue nesting capped at 32; processing is in memory.

The default resource-label snapshot was retrieved **2026-11-11T19:02:07Z** from [Loki's shared OTLP documentation](https://raw.githubusercontent.com/grafana/loki/main/docs/sources/shared/otel.md), with source SHA-256 `be9d44ae00e13ed3773441aaf4e05bfcb6a367715eef7c5561f5c95d30a788d3`. Its 18 exact keys and source metadata are bundled in `src/otelwrench/loki/defaults.json`. `main` documentation changes over time; this tool never fetches defaults at runtime.

Format references: [Loki OTLP configuration](https://grafana.com/docs/loki/latest/send-data/otel/), [push JSON API](https://grafana.com/docs/loki/latest/reference/loki-http-api/#ingest-logs), [OTLP log schema](https://github.com/open-telemetry/opentelemetry-proto/blob/main/opentelemetry/proto/logs/v1/logs.proto), [AnyValue schema](https://github.com/open-telemetry/opentelemetry-proto/blob/main/opentelemetry/proto/common/v1/common.proto). Consult [THIRD_PARTY.md](../THIRD_PARTY.md) for the separation between references and dependencies.

## Tests

The suite in `tests/loki/` exercises real JSON/YAML parsing, CLI subprocesses, independently authored golden output (`examples/loki/expected-push.json`, `expected-topology.json`, `expected-decisions.json`), rule precedence, normalization collisions, nanosecond precision and failure paths. Run it with `python -m pytest tests/loki` from a clone with `pip install -e ".[test]"`.

License and attribution: see [THIRD_PARTY.md](../THIRD_PARTY.md).
