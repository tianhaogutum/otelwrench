# otelwrench cardinality

Offline, collision-aware what-if removal of labels from saved Prometheus
scrapes, individually and over an ordered set of identified captures. Reports
observed series and per-family distinct label values, and lists
**every collision with every original witness**. Equal sample values are still
unsafe. No samples are combined or rewritten, and no service is contacted.

## When to use it

Use `otelwrench cardinality` before adding a `metric_relabel_configs` rule that
drops a label to cut cardinality: it shows, on real saved scrapes, whether removing
that label would merge distinct series (a silent data loss), and emits a candidate
relabel rule for the plans that are safe on the observed data.

## Quick start

Install with `pip install otelwrench` (this command needs only the Python standard
library). The samples live in [`examples/cardinality/`](../examples/cardinality/), so
run the commands from a clone of the repository.

```sh
otelwrench cardinality examples/cardinality/metrics.prom --drop-labels debug_id --format json
```

This reports 5 → 5 observed keys, `status: observed_safe`. `otelwrench cardinality
--version` prints `otelwrench cardinality 0.1.0`.

```sh
# Baseline statistics only
otelwrench cardinality examples/cardinality/metrics.prom
# Independent plans against the same input (this returns 1 due to plan 2)
otelwrench cardinality examples/cardinality/metrics.prom --drop-labels debug_id --drop-labels pod,debug_id --format json
# Include candidate YAML inside the report; this writes only stdout
otelwrench cardinality examples/cardinality/metrics.prom --drop-labels debug_id --metric requests_total --emit-config --format json
# Four original series, two projected keys, equal and different collisions; exit 1
otelwrench cardinality examples/cardinality/collisions.prom --drop-labels pod --emit-config --format json
# Classic histogram and summary families, retaining protected le/quantile labels
otelwrench cardinality examples/cardinality/families.prom --drop-labels debug_id --format json
```

Repeat `--metric` for exact **sample names**, not family names or regex patterns.
Repeated `--drop-labels` compares independent user supplied plans, never a search
or cumulative transformation. Named plans can instead come from
`--candidate-file` (below). Omit both options for baseline statistics only.

## Ordered scrape sets and observed union

Use `--scrape-set` instead of the positional input to compare the same explicit
label-drop candidates per scrape **and** across all observed canonical identities.
The local [example manifest](../examples/cardinality/scrape-set.json) has this exact shape:

```json
{
  "schema_version": 1,
  "scrapes": [
    {"id": "morning", "source": "target-a", "captured_at": "2026-11-11T08:00:00Z", "path": "scrape-before.prom"},
    {"id": "evening", "source": "target-a", "captured_at": "2026-11-11T18:00:00+01:00", "path": "scrape-after.prom"}
  ]
}
```

Each entry requires all four fields; optional `target_labels` is described below.
IDs must be unique, sources nonempty, and
`captured_at` a valid timestamp of the form
`YYYY-MM-DDTHH:MM:SS[.fraction](Z|±HH:MM)` (no leap seconds). Capture time is
separate from each sample's optional millisecond timestamp; both tokens are
preserved. Input order is retained, even when times are out of order. Repeated
sources and paths are allowed. Relative paths resolve beside the manifest;
absolute paths are allowed. Each file must satisfy the existing nonempty text
0.0.4 subset. Unknown/duplicate JSON fields, wrong schema versions, duplicate IDs,
bad timestamps, unreadable files or any invalid scrape fail the entire request
with exit 2 and **no stdout**. Stdin is available only in single-scrape mode.

```sh
# Each scrape has one identity: locally safe; the union has two -> one: exit 1
otelwrench cardinality --scrape-set examples/cardinality/scrape-set.json --drop-labels pod --format json
# Removing only an explicitly empty label is safe across this observed set: exit 0
otelwrench cardinality --scrape-set examples/cardinality/scrape-set.json --drop-labels zone
# Rank zone; exclude pod despite each individual scrape passing: exit 1
otelwrench cardinality --scrape-set examples/cardinality/scrape-set.json --rank-plans \
  --drop-labels pod --drop-labels zone --format json
```

The first capture contains `requests_total{pod="a",zone=""} 7 100`; the second
contains `requests_total{pod="b"} 7.0 200`. Dropping `pod` reports
`all_scrapes_safe: true`, union `status: unsafe_cross_scrape`, and a collision
classified `cross_scrape_only` with both complete witnesses. Equal values and
different timestamps do not excuse identity merging. A group that already
collides inside any individual scrape is classified `within_scrape`; a plan with
any such group has `status: unsafe`. `cross_scrape_collision_group_count` counts
only groups that are collision-free in every individual scrape.

Scrape-set JSON reports use **schema version 2**. `scrapes` retains ordered
`id`/`source`/`captured_at`, independent summaries (including family metadata), and
per-scrape plans. Top-level `plans` checks the observed union: before/after counts
and `metric_counts` count **distinct canonical series identities**, not samples.
Union family metadata is not synthesized when declarations differ across files.
`summary` gives `scrape_count`, `sample_observations`, and distinct
`observed_series`. Each union collision reports `original_series_count`,
`sample_observation_count`, and **every** original observation with `scrape_id`,
`source`, `captured_at`, original labels/key, value token, sample timestamp and line.
Text output also includes per-scrape results and every union witness.

Repeated observations of the **same canonical identity** are not a collision,
even if their values, timestamps or explicit empty-label spelling change. An empty
label still has absent-label identity. Source, ID and capture time are provenance,
**not implicit series labels**: include actual target labels in saved exposition
or supply explicit `target_labels` to make them part of identity. Cross-source
overlap is checked against exactly the labels supplied; there is no inference
about real target identity or simultaneity.
No sample value is merged, deduplicated, exported, or used to resolve a collision.

Labels and exact metric scope must exist in the selected **union**; they may be
absent in individual scrapes, where the rule has no effect. Plans remain
independent. Optional ranking retains the same 64-candidate/8-label limits and
excludes every union-unsafe candidate. Its label-pair score sums **sample
observations across scrapes**, including repeated identities and explicit empties;
`candidate_ranking.scoring_scope` makes this explicit. It is not a distinct-identity
or storage-saving score. `--emit-config` is unsupported for scrape sets (exit 2).
The library API is `analysis.report_scrapes([scrapes.IdentifiedScrape(id, source,
captured_at, parsed_scrape), ...], plans, ...)`; `scrapes.load_scrape_set(path)`
loads the same manifest. **Observed-safe applies only to the supplied captures;
no future-safety guarantee is made.**

### Explicit target-label context

Manifest v1 entries can supply optional `target_labels`, a JSON object of label
names to string values. This lets two exporter captures with identical exposition
be checked as distinct target identities. The [target example](../examples/cardinality/target-scrape-set.json)
uses the same [saved exporter text](../examples/cardinality/exporter.prom) twice:

```json
{
  "schema_version": 1,
  "scrapes": [
    {"id": "a", "source": "exporter", "captured_at": "2026-11-11T08:00:00Z", "path": "exporter.prom", "target_labels": {"job": "api", "instance": "a"}},
    {"id": "b", "source": "exporter", "captured_at": "2026-11-11T08:00:00Z", "path": "exporter.prom", "target_labels": {"job": "api", "instance": "b"}}
  ]
}
```

Context is merged into every sample **for analysis only**, before summaries,
scope/label validation, projection, union identity and ranking. Label-pair scores
count these effective labels, once per sample; an equal exposition/context label
is counted once. Input text and parsed exposition are preserved. Omitted context
and `{}` have identical behavior; source/ID/capture time still add no labels.

Conflict policy is strict: an explicitly present exposition label must have
exactly the same decoded string value as its context label. Equal values are
accepted; different values reject the entire request, even on an unselected
metric or in a later capture. Explicit empty and nonempty values conflict; an
absent label can receive either. If context makes two samples within a capture
share a canonical identity, that is duplicate **input**, rejected before any plan
is evaluated, regardless of values or timestamps. Labels are never renamed or
overwritten; there is no configurable `honor_labels` simulation.

Names must use legacy ASCII label syntax; `__*`, `le` and `quantile` are rejected
as context labels. Values must be strings, including empty strings and Unicode;
JSON escapes for LF, quotes and backslashes are supported. NUL, CR and unpaired
surrogates are rejected. Wrong types, invalid/protected names, duplicate JSON
keys, conflicting values and induced duplicate series fail atomically with
**exit 2, empty stdout**, and an error on stderr. No context is inferred from a
path, hostname or provenance field.

```sh
# Two effective identities -> one: each local plan safe, union unsafe; exit 1
otelwrench cardinality --scrape-set examples/cardinality/target-scrape-set.json --drop-labels instance --format json
# Removing exporter debug labels retains both target identities; exit 0
otelwrench cardinality --scrape-set examples/cardinality/target-scrape-set.json --drop-labels debug --format json
```

Report schema v2 is retained. Entries with nonempty context include `target_labels`
in JSON and text (including baseline-only reports). Every collision witness for
such an entry includes `target_labels` and decoded `exposition_labels`.
`original_labels` and `original_key` describe the **effective pre-drop** labels
and canonical identity; value/timestamp/line and provenance remain unchanged.
Both per-scrape and union witnesses retain this evidence. The library constructor
accepts `IdentifiedScrape(..., target_labels={"job": "api", "instance": "a"})`;
its `.scrape` and `load_scrape_set` records retain parsed exposition. Context is
validated by the loader and applied to an analysis copy by `report_scrapes`.
This models only explicitly supplied label context, not service discovery,
relabeling, actual Prometheus ingestion or unseen targets.

### Machine-readable target-label conflicts

An explicit unequal exposition/context overlap retains **exit 2, empty stdout**
and the exact text error. With `--format json`, stderr also includes `details`:

```json
{
  "error": "scrape 'late', line 3: target label 'job' conflicts with exposition",
  "error_type": "invalid_or_unsupported_input",
  "details": {
    "code": "target_label_conflict",
    "scrape_id": "late",
    "line": 3,
    "label": "job",
    "exposition_value": "other",
    "target_value": "api"
  }
}
```

This example describes an explicitly present `job="other"` on original sample
line 3 with supplied `target_labels: {"job": "api"}`. Values are the exact decoded
strings, including Unicode, LF, quotes, backslashes and empty strings; they are
JSON-escaped in stderr. Library callers receive the same dict as
`InputError.details`; `str(exc)` and `exc.args` keep the original message.

Only the first conflict is returned, in **capture order, sample order, then
context mapping insertion order** (JSON field order for manifest context).
Context labels are not sorted for conflict selection. All names/values in a
capture's context validate before its samples are merged. Context-induced
duplicates and conflicts retain their sample traversal precedence; malformed
context keeps generic errors with `details=None`. Induced duplicates have the
structured evidence described below.
Late captures and unselected metrics still reject before summaries, projection
or ranking. Equal decoded overlaps are accepted once; explicit empty versus
nonempty values conflict. Successful reports and all other structured evidence
are unchanged. No new conflict policy or flags are introduced.

### Machine-readable context-induced duplicate input

When target context makes two samples in one capture share a canonical identity,
the request retains **exit 2, empty stdout** and the exact text error. JSON stderr
also includes `details`, identical to the library's `InputError.details`:

```json
{
  "error": "scrape 'late', line 3: duplicate input series after target labels; first seen on line 1",
  "error_type": "invalid_or_unsupported_input",
  "details": {
    "code": "target_label_duplicate",
    "scrape_id": "late",
    "line": 3,
    "first_seen_line": 1,
    "effective_key": {"metric": "x", "labels": {"instance": "a"}}
  }
}
```

For example, original lines `x{instance="a",zone=""} 7 100`, `# gap`, and
`x 9 200` with `target_labels: {"instance": "a", "zone": ""}` produce this error.
Line numbers refer to the saved exposition, including comments and blank lines.
The effective key uses decoded post-merge label values, including Unicode, LF,
quotes and backslashes; empty values are omitted from this canonical key.
It is not a raw label map. Raw parsed samples and supplied context are unchanged.

Only the first duplicate is returned, in capture then sample order. The first
sample's line is retained even with more than two colliding samples. Equal or
different values and timestamps never permit duplicate input. Every context
name/value in the current capture validates before merging; a conflict on a
sample precedes its duplicate check, and an earlier duplicate precedes later
conflicts. Late captures and unselected metrics still reject before coverage,
summaries, projection or ranking, with inline and v1/v2 candidates alike.

`str(exc)`, `exc.args` and text stderr remain unchanged. No flag, duplicate policy,
deduplication or sample rewriting is added. Malformed-context errors remain
generic (`details=None`). Original exposition duplicates have separate evidence
as described below.
Successful reports, scores, ranks, witnesses and existing conflict, coverage and
protected-removal evidence are unchanged.

### Machine-readable original input duplicates

Duplicate series already present in a single saved exposition fail in `parse`,
before target context or analysis. Single-file and stdin CLI input retain
**exit 2, empty stdout** and the exact text error. JSON stderr adds `details`,
also available as the library's `InputError.details`:

```json
{
  "error": "line 3: duplicate input series; first seen on line 1 (timestamps do not change series identity)",
  "error_type": "invalid_or_unsupported_input",
  "details": {
    "code": "duplicate_input_series",
    "line": 3,
    "first_seen_line": 1,
    "original_key": {"metric": "x", "labels": {"instance": "a"}}
  }
}
```

For example, `x{instance="a",zone=""} 7 100`, `# gap`, and
`x{instance="a"} 9 200` produce this diagnostic. `original_key` is the canonical
decoded exposition identity: labels are sorted and empty values omitted. Unicode,
LF, quotes and backslashes remain decoded values in library evidence and are
JSON-escaped on stderr. Values and timestamps never distinguish series.

For manifest v1, the loader preserves these details and adds the supplied
`scrape_id`. Its existing error wrapper stays exact, for example
`scrape 'late' (late.prom): line 3: duplicate input series; first seen on line 1 (timestamps do not change series identity)`.
Both line numbers refer to that saved file, including comments and blank lines.
The wrapper copies the details without changing the underlying parser error.
Context-induced duplicates keep `target_label_duplicate` and `effective_key`.

Only the first duplicate is reported, in existing capture/sample traversal order,
even with more than two repetitions or multiple duplicate groups. Late captures
and unselected metrics still reject before summaries, comparison or ranking,
including inline and v1/v2 candidates. Existing syntax, family, metadata, manifest
and option validation precedence is unchanged; those errors can be reported first.
Unrelated errors remain generic. Exact text/args, successful reports, scores, ranks
and witnesses are unchanged. No flag, overwrite or deduplication policy is added.

### Machine-readable duplicate label names

A repeated label name within one exposition sample fails in `parse`, even when
both values are equal or empty. For `x{a="one",a="two"} 1`, JSON stderr is:

```json
{
  "error": "line 1: duplicate label 'a'",
  "error_type": "invalid_or_unsupported_input",
  "details": {"code": "duplicate_input_label", "line": 1, "label": "a"}
}
```

The same details are available on `InputError.details`; `str(exc)` and `exc.args`
retain the exact original message. File and stdin CLI input retain **exit 2,
empty stdout**, and text stderr remains `otelwrench cardinality: line 1: duplicate label 'a'`.
Manifest v1 copies the evidence and adds the supplied `scrape_id`, retaining the
exact `scrape 'late' (late.prom): line 1: duplicate label 'a'` wrapper. The line
is the original saved-file line, including comments and blanks; the parser cause
keeps its own details without `scrape_id`.

The first repeated name wins in sample/label input order, before parsing its
repeated assignment or value: even `x{a="one",a=unquoted} 1` reports the duplicate.
Earlier syntax errors and existing manifest, metadata and family validation
precedence are unchanged. Late captures and unselected metrics still reject
before analysis, comparison or ranking, including inline and v1/v2 candidates.
This is distinct from duplicate series: those keep `duplicate_input_series` /
`original_key`, or `target_label_duplicate` / `effective_key` after target context.
Unrelated errors remain generic; successful output bytes and label policy are
unchanged. No repeated-value evidence or new flags are added.

### Machine-readable reserved exposition label names

Exposition labels starting with `__`, including `__name__` and `__meta_*`,
remain unsupported. For `x{__name__="other"} 1`, JSON stderr is:

```json
{
  "error": "line 1: reserved/internal label '__name__' is unsupported",
  "error_type": "invalid_or_unsupported_input",
  "details": {"code": "reserved_input_label", "line": 1, "label": "__name__"}
}
```

`InputError.details` exposes the same evidence; `str(exc)` and `exc.args` retain
the exact message. File/stdin CLI input retains **exit 2, empty stdout**, and
unchanged text stderr. Manifest v1 copies the details and adds the supplied
`scrape_id`, preserving the exact `scrape 'late' (late.prom): line 1:
reserved/internal label '__name__' is unsupported` wrapper and parser cause.
The line is the original saved-file line, including comments and blank lines;
the cause retains its own details without `scrape_id`.

The first reserved name encountered rejects before parsing its assignment or
value, so `x{__name__=unquoted} 1` gives the same evidence. Earlier syntax,
metadata, repeated-label and manifest errors retain their precedence. Late
captures and unselected metrics reject before analysis, comparison or ranking,
including scoped inline and v1/v2 candidates. Existing duplicate-series,
duplicate-label and target/context diagnostics remain distinct. Other errors
remain generic and successful output bytes are unchanged. No internal labels
are accepted, no flags are added, and target-label policy is unchanged.

### Machine-readable invalid sample values and trailing data

Malformed or unsupported value tokens and excess trailing tokens still fail in
`parse`. For `x nope` or `x 1 # {trace_id="a"} 2`, JSON stderr is:

```json
{
  "error": "line 1: invalid value or unsupported trailing data (e.g. exemplar)",
  "error_type": "invalid_or_unsupported_input",
  "details": {"code": "invalid_input_sample_value", "line": 1}
}
```

The library exposes the same `InputError.details`, retaining exact `str(exc)` and
`exc.args`. File/stdin CLI input retains **exit 2, empty stdout** and unchanged
text stderr. Manifest v1 copies the evidence and adds the supplied `scrape_id`,
preserving the existing `scrape 'late' (late.prom): line 1: invalid value or
unsupported trailing data (e.g. exemplar)` wrapper and parser cause. The line
refers to the original saved file, including comments and blanks; the cause's
details remain separate and do not acquire `scrape_id`.

This evidence identifies the existing value/token-count rejection branch only;
it does not classify the token or include raw value/exemplar data. Bare missing
values (`x` or `x   `) still fail earlier with the generic whitespace-separation
error. A valid value followed by a single invalid timestamp (`x 1 nope` or
`x 1 #`) gives the structured timestamp error below; decimal float64 overflow
(`x 1e999`) has separate structured evidence below. With more than two tokens, the
value/token-count branch still wins before timestamp and overflow checks.

Earlier label, syntax, metadata, newline and manifest errors retain their
precedence. Late captures and unselected metrics reject before analysis,
comparison or ranking, including scoped inline and v1/v2 candidates. Existing
label, series and target/context evidence and successful output bytes are
unchanged. No numeric forms, exemplars, flags or other syntax are newly accepted.

### Machine-readable invalid sample timestamps

A valid sample value followed by exactly one malformed timestamp still fails with
`timestamp must be an integer in milliseconds`; a signed int64 overflow still
fails with `timestamp outside signed int64 range`. For `x 1 nope`, JSON stderr is:

```json
{
  "error": "line 1: timestamp must be an integer in milliseconds",
  "error_type": "invalid_or_unsupported_input",
  "details": {
    "code": "invalid_input_sample_timestamp",
    "line": 1,
    "reason": "invalid_integer"
  }
}
```

`reason` is `invalid_integer` for tokens outside ASCII `[+-]?[0-9]+`, including
Unicode digits, fractional and exponent forms, or `out_of_range` for integers
below -9223372036854775808 or above 9223372036854775807. Bounds are inclusive;
optional signs and leading zeros remain accepted and their exact spelling is
preserved. Range checking still compares strings before any integer conversion,
including arbitrarily long signed/zero-padded tokens. No timestamp syntax or
flags are added.

Library `InputError.details` contains the same evidence; `str(exc)` and `exc.args`
retain their exact messages. File/stdin CLI input retains **exit 2, empty stdout**
and unchanged text stderr. Manifest v1 copies the details and adds the supplied
`scrape_id`, preserving the existing wrapper, for example
`scrape 'late' (late.prom): line 1: timestamp must be an integer in milliseconds`.
`line` is the original saved-file sample line, including comments and blanks.
The parser cause keeps its separate details without `scrape_id`.

Late captures and unselected metrics reject before analysis, comparison or
ranking, including scoped inline and v1/v2 candidates. Invalid first values or
excess tokens retain `invalid_input_sample_value`; decimal float64 overflow is
checked before timestamps and retains its separate error below. Earlier label,
separator, metadata, newline and manifest errors retain their precedence, as do
all prior contextual diagnostics. Successful output bytes remain unchanged.

### Machine-readable decimal sample overflow

A lexically valid decimal sample value whose float64 conversion is infinite
still fails with `decimal value overflows float64; use explicit Inf`.
For `x 1e999`, JSON stderr is:

```json
{
  "error": "line 1: decimal value overflows float64; use explicit Inf",
  "error_type": "invalid_or_unsupported_input",
  "details": {"code": "input_sample_value_overflow", "line": 1}
}
```

Library `InputError.details` contains the same evidence; `str(exc)` and `exc.args`
retain the exact message. File/stdin CLI input retains **exit 2, empty stdout**
and unchanged text stderr. Manifest v1 copies the evidence and adds the supplied
`scrape_id`, preserving the wrapper, for example
`scrape 'late' (late.prom): line 1: decimal value overflows float64; use explicit Inf`.
`line` identifies the original sample line, including comments and blanks.
The parser cause keeps separate details without `scrape_id`.

This identifies only the existing decimal overflow rejection, including signed,
exponent and long decimal forms; it does not include the raw value. Explicit
`NaN`, `Inf`, `+Inf`, `-Inf`, finite decimals, signed zero and decimal underflow
remain accepted with exact value spelling preserved. No numeric syntax, flags
or conversion policy changes. Invalid VALUE tokens or excess tokens retain
`invalid_input_sample_value`; overflow still precedes optional timestamp checks.
Earlier label, separator, metadata, newline and manifest errors retain their
precedence. Late captures and unselected metrics reject before analysis,
comparison or ranking, including scoped inline and v1/v2 candidates. Prior
contextual evidence and successful output bytes remain unchanged.

### Required target-label coverage (opt-in)

Repeat `--require-target-label LABEL` with `--scrape-set` to require nonempty
identity labels on **every effective sample**, including baseline-only reports:

```sh
# Both targets have job/instance; debug removal retains two identities; exit 0
otelwrench cardinality --scrape-set examples/cardinality/target-scrape-set.json \
  --require-target-label job --require-target-label instance --drop-labels debug --format json
```

Each label can come from explicit context or exposition. Coverage is checked
after their strict merge, before label removal, independently of candidate metric
scopes and ranking. A label present elsewhere in the observed union does not
cover a sample where it is missing. A missing or explicitly empty value in any
capture or metric fails the **whole request with exit 2 and empty stdout**.
Stderr identifies the scrape ID, original line and required label, for example
`scrape 'late', line 3: required target label 'instance' is absent or empty`.
The first gap is reported in capture/sample order, with label names sorted.
Whitespace-only values are nonempty; provenance never supplies coverage.

Names must be distinct legacy ASCII label names, excluding `__*`, `le` and
`quantile`. Each flag takes one exact name, with no trimming or comma splitting;
invalid/protected/duplicate names and single-scrape file/stdin usage fail with
exit 2 and no stdout. The library option is
`analysis.report_scrapes(scrapes, plans, required_target_labels=["job", "instance"], ...)`.
It accepts a list or tuple of distinct strings (default `()`); other containers,
`None` and nonstrings raise `InputError`.

Scrape-set JSON retains schema v2 and adds sorted `required_target_labels`, an
empty array when omitted. Text reports show nonempty requirements. With no
opt-in, absent/empty labels and omitted/empty context keep their prior behavior.
Coverage changes no projections, scores or witnesses and does not prevent a plan
from dropping a required label (unless also protected below). It verifies supplied pre-drop label coverage,
not actual target identity, uniqueness, or safety beyond the observed captures.

### Protecting target labels from removal (opt-in)

Repeat `--protect-target-label LABEL` with `--scrape-set` to require nonempty
coverage **and** forbid removal of those labels by any candidate:

```sh
# Protect both supplied target identities while trimming debug; exit 0
otelwrench cardinality --scrape-set examples/cardinality/target-scrape-set.json \
  --protect-target-label job --protect-target-label instance --drop-labels debug --format json
```

The library option is `report_scrapes(..., protected_target_labels=["job", "instance"])`,
default `()`. It uses the same distinct-name, list/tuple and single-scrape CLI
restrictions as required labels. Required and protected lists may overlap;
**required-only labels remain droppable**. Coverage checks the sorted union of
both lists on every effective sample, including baseline-only reports, late
captures and unselected metrics. Missing/empty protected coverage uses the same
`required target label 'instance' is absent or empty` diagnostic, with scrape ID
and line. Provenance cannot supply coverage.

After candidate input/scope validation and before any projection, a candidate
dropping a protected label fails the entire request with **exit 2, empty stdout**
(library `InputError`), regardless of metric scope or ranking. Stderr identifies
the candidate name (or generated `plan-N`) and label, for example
`candidate 'Late identity': cannot remove protected target label 'instance'`.
The first failing candidate is reported in input order, with forbidden names
sorted. Invalid scrape/options/candidate input may be diagnosed first.

Schema v2 adds sorted `protected_target_labels`, `[]` by default; explicit
`required_target_labels` remains separate. Text shows nonempty protections.
Allowed candidates retain the same projections, scores, rankings and every
witness. With no opt-in, no label is newly protected. Protection verifies supplied
coverage and enforces a removal policy; it does not establish real target
identity, uniqueness or future safety.

### Machine-readable coverage gaps

With `--format json`, missing/empty required or protected coverage retains exit 2,
empty stdout and the existing `error`/`error_type` fields on stderr. It also adds
`details`, for example:

```json
{
  "error": "scrape 'late', line 3: required target label 'instance' is absent or empty",
  "error_type": "invalid_or_unsupported_input",
  "details": {
    "code": "target_label_coverage",
    "scrape_id": "late",
    "line": 3,
    "label": "instance",
    "reason": "empty"
  }
}
```

`reason` is `absent` when the label is missing from the effective sample, or
`empty` when exposition, target context or their equal overlap explicitly supplies
`""`. Empty values remain distinguishable here even though canonical series keys
omit them. Whitespace is nonempty. `line` is the original sample line, and
`scrape_id` is the supplied ID, never inferred from provenance.

Library callers catching `otelwrench.cardinality.parser.InputError` receive the same dict
as `exc.details`; `str(exc)` and `exc.args` retain the original message.
Text stderr remains unchanged. Only the first gap is reported,
in capture/sample/sorted-label order over the union of required and protected
names. Overlapping declarations do not duplicate checks. No summary or candidate
projection is produced before coverage succeeds; successful reports are unchanged.

### Machine-readable protected removals

A candidate removing a protected target label also adds `details` to JSON stderr
and `InputError.details`, retaining exit 2, empty stdout and the exact text error:

```json
{
  "error": "candidate 'Late identity': cannot remove protected target label 'instance'",
  "error_type": "invalid_or_unsupported_input",
  "details": {
    "code": "protected_target_label_removal",
    "candidate_name": "Late identity",
    "candidate_index": 2,
    "label": "instance",
    "scope_metric_names": ["y", "z"]
  }
}
```

`candidate_index` is the **one-based input position**, independent of ranking.
`candidate_name` preserves the supplied name exactly, or the generated `plan-N`
for inline plans. `scope_metric_names` is the sorted, validated exact metric scope:
the candidate's v2 scope, the shared v1/inline `--metric` scope, or all observed
union metric names when scope is omitted. Scope does not waive protection.

Only the first forbidden removal is reported, in candidate input order then sorted
label order. All candidate/name/scope validation completes before removal checks;
existing input, merge, coverage and option errors may take precedence. No summary,
projection or ranking is produced on rejection. Required-only labels remain
droppable, and allowed/default reports, scores, ranks and witnesses are unchanged.
Other input errors retain `details=None` and the JSON `error`/`error_type` shape.
Coverage gaps, protected target-label removals, explicit context/exposition
conflicts, context-induced duplicates, original input duplicates, repeated and
reserved label names, invalid sample value/trailing-data errors, decimal sample
overflow and invalid sample timestamps have
structured evidence as described above.

## Bounded candidate ranking

### Named candidate files

Use `--candidate-file candidates.json` instead of `--drop-labels` to load an
ordered list of independently evaluated, user-named plans. The file has this
exact versioned input shape (separate from the output report's schema version):

```json
{
  "schema_version": 1,
  "candidates": [
    {"name": "Trim redundant debug", "drop_labels": ["debug"]},
    {"name": "Review interaction", "drop_labels": ["a", "b"]}
  ]
}
```

For v1, both fields on each candidate are required; no other fields are accepted. Names
must be unique, case-sensitive strings of 1–64 printable characters with no
leading/trailing whitespace. Spaces and Unicode are allowed; names are preserved
exactly. `drop_labels` must be a nonempty array of distinct, valid label-name
strings; labels are not whitespace-trimmed. Protected or absent labels still fail
under the existing metric-scope rules. Reordering labels does not create a new
combination: duplicate combinations are rejected even when names differ.

File input **always** enforces at most 64 candidates and 8 labels per candidate,
including comparison without `--rank-plans`. Only one candidate file may be
supplied; it cannot be combined with any `--drop-labels`. The candidate file must
be a local UTF-8 JSON file, never stdin (`-`); single-scrape exposition may still
come from stdin. Unknown or duplicate fields at any depth, malformed JSON,
nonstandard NaN/Infinity constants, wrong versions/types, empty arrays, invalid
names/labels, repeated names/combinations, unreadable files and over-limit files
fail the whole request with **exit 2, empty stdout**, and an error on stderr.
This includes an invalid candidate after valid ones; nothing is skipped.

Works with a single scrape or `--scrape-set` and optional `--rank-plans`.
V1 shares the optional global `--metric` scope (default: all observed names). Output schemas stay unchanged: names replace `plan-N` in every
top-level plan, per-scrape plan, ranking entry and text report. Candidate order is
preserved in `plans`; ranking uses the existing score and label-name tie-break,
independent of plan names. Valid all-safe files exit 0; any unsafe plan exits 1
with every witness. Existing config rules apply: only one non-ranking,
single-scrape plan may use `--emit-config`; unsafe plans emit no YAML.

```sh
# Named plans in file order; exit 1 for the unsafe interaction
otelwrench cardinality examples/cardinality/ranking.prom --candidate-file examples/cardinality/candidates.json --format json
# Rank the safe named plan, retaining the unsafe plan and all witnesses; exit 1
otelwrench cardinality examples/cardinality/ranking.prom --candidate-file examples/cardinality/candidates.json --rank-plans --format json
```

Library callers can use `candidates.load_candidates(path)` to get ordered
`Candidate(name, drop_labels, metrics=None)` records for v1, then pass label arrays
and `plan_names=` to `analysis.report` or `analysis.report_scrapes`. Named library plans enforce
the same candidate/name bounds. Omitting names retains generated `plan-N` names
and the existing ordinary comparison behavior.

### Per-candidate exact metric scopes (candidate-file v2)

Use input `schema_version: 2` when each named candidate needs its own scope:

```json
{
  "schema_version": 2,
  "candidates": [
    {"name": "Safe pod", "drop_labels": ["pod"], "metrics": ["safe_metric"]},
    {"name": "Risky pod", "drop_labels": ["pod"], "metrics": ["risky_metric"]}
  ]
}
```

All three fields are required and no others are accepted. `metrics` is a
nonempty array of **distinct exact sample names**, using the supported legacy
ASCII name syntax. Names are not trimmed; regexes and family-name expansion are
unsupported. Every selected name must occur in the single scrape or observed
union, and every dropped label must occur within that candidate's scope. A
selected metric or label may be absent in an individual scrape of a valid set.
V2 rejects any global `--metric`, even if it names the same scope. V1 still
rejects a per-candidate `metrics` field and keeps its shared-scope behavior.

The 64-candidate/8-label bounds and unique-name rules apply to both versions.
V2 combination identity is **(sorted drop_labels, sorted metrics)**: the same
labels in different scopes are allowed, but reordering either array does not
create a distinct combination. Invalid, empty, duplicate or unobserved scopes,
labels absent from their selected scope, unknown fields and conflicting options
fail atomically with **exit 2 and empty stdout**, including a late invalid entry.

Each plan's sorted `scope_metric_names` controls label removal, scores, and
collision evidence independently. Whole-input summaries and before/after totals
still include unchanged metrics; those metrics retain their original identities
and label pairs. This preserves the existing output report schemas (1 for a
single scrape, 2 for a set). Names and plan order remain as supplied in local and
union reports, with scope also shown in text. Ranking keeps score-descending then
label-name ordering; complete ties retain input order, with no name or metric
tie-break. Every observation still counts toward union scores.

```sh
# Same label safe on one metric, unsafe on another; 6 plans, 2 unsafe; exit 1
otelwrench cardinality examples/cardinality/scoped-metrics.prom --candidate-file examples/cardinality/scoped-candidates.json --rank-plans --format json
```

The example projects **5, 4, 4, 5, 5, 5** keys and removes **2, 2, 4, 2, 2, 4**
label pairs. `Risky pod` and `Both pods` retain both unequal-value witnesses for
`risky_metric`; `Safe pod` preserves all five identities. Safe ranking is
`Safe pair` (4), `Safe debug` (2), `Risky debug` (2), `Safe pod` (2).
Single-plan config emission uses the candidate's exact scope and retains all
existing safety/config restrictions.

Library v2 records have `Candidate.metrics` tuples. Pass these as aligned
`plan_metrics=[candidate.metrics for candidate in candidates]` together with
labels and `plan_names=` to either report function. `plan_metrics` takes a list
or tuple with one nonempty list/tuple of distinct exact names per plan, enforces
the same bounds/combination identity, and cannot be combined with `metrics=`.
For v1, omit `plan_metrics` (the records' `metrics` values are `None`). All
candidates are validated before projection.

### Ranking explicit plans

Use `--rank-plans` to evaluate **only the supplied combinations**, independently
against the unchanged input, and rank the collision-free candidates. There is no
automatic subset generation. Hard limits are **64 candidates** and **8 labels per
candidate**, checked before projection. Repeated combinations (ignoring label
order, and also metric order for v2), absent/protected labels, invalid scope, empty candidates, or exceeding
either limit fail the entire request with exit 2 and no stdout. Nothing is silently
skipped or truncated. These limits apply to ranking and candidate-file mode;
ordinary unnamed `--drop-labels` comparison retains its existing behavior.

```sh
# Six supplied plans; exit 1 because two are unsafe, even though four can be ranked
otelwrench cardinality examples/cardinality/ranking.prom --rank-plans \
  --drop-labels a --drop-labels b --drop-labels a,b \
  --drop-labels debug --drop-labels b,debug --drop-labels debug,region --format json
```

The six-series fixture has a single-label collision on `y` and an interaction
collision on `x`: dropping `a`, `b`, and `a,b` yields exactly **5, 6, and 4**
projected keys. The unsafe plans remain in `plans` with every witness, including
equal values with equal or different timestamps. They never appear in the ranking.

The ranking objective is **explicit label pairs removed**, descending: each label
occurrence on an original sample counts once, including explicitly empty values.
Only labels in the selected metric scope are removed. Counts are computed before
any hypothetical key merging; witnesses and input samples are unchanged. The
example ranks `debug,region` (10), `b,debug` (8), `debug` (6), then `b` (2). All four
retain 6 projected series. Ties use the sorted label names lexicographically,
independent of candidate order. Unnamed `--drop-labels` plans use `plan-N` for the
original argument position; candidate-file names are preserved. This measures
label information removed, **not** series, bytes, memory,
cost, or performance savings.

JSON adds `candidate_ranking` with limits, evaluated/excluded counts, objective,
tie-break rule, and `ranked_safe_plans` (rank, plan name, labels, score). Every plan
also reports `label_pairs_before`, `label_pairs_after`, and `label_pairs_removed`,
counting occurrences across all original samples, including unchanged metrics.
An all-unsafe set has an empty ranking and exit 1. An all-safe set exits 0.
Text output includes the same ranking and full collision evidence. Ranking cannot
be combined with `--emit-config` (exit 2); review any chosen explicit plan in a
separate invocation. Ranking never emits or applies configuration.

Exit codes: **0** valid summary/all plans observed-safe; **1** any collision;
**2** malformed/unsupported input or invalid options. JSON reports go to stdout,
input errors to stderr. `--emit-config` requires one plan. Unsafe plans still
report all witnesses, include `config_refusal`, emit no YAML, and exit 1.

Single-scrape JSON schema version 1 includes `summary.families` (observed series, all distinct
label values and presence/absence counts), `summary.declared_metadata` (including
metadata for families with no samples), and independent `plans`. Each plan has
before/after key counts, `observed_cardinality_delta` (after minus before), affected
and collision metric names, per-family/metric counts, `safe`/`unsafe`, and every
collision's projected key plus original keys/labels/values/timestamps/line numbers.
The report is deterministic for unchanged input and options; it has no generated
run timestamps or runtime measurements. `--format text` also lists every
collision witness.

## Reproducible experiment

```sh
python benchmarks/cardinality/experiment.py --routes 100 --pods 30 --repeats 3
```

This generates real exposition text and checks `otelwrench cardinality` against an
independent oracle, including every collision witness. The explicit baseline
keeps all labels; two explicit plans demonstrate safe removal and unsafe merging.
Measured wall times, input hash, exact counts, methodology and limitations are in
[benchmarks/cardinality/README.md](../benchmarks/cardinality/README.md) and
[benchmarks/cardinality/results.json](../benchmarks/cardinality/results.json). No production gain is claimed.

## Safety and limitations

A collision-free deletion cannot lower the number of observed series: a decrease
means distinct original series merged. Removing redundant labels can reduce
label information but is not a proven TSDB series, memory, cost or speed saving.
`safe: true` means **only no collision in the supplied observed scrape or set**.
Unobserved future scrapes, other targets, unsupplied target labels, existing relabel rules
and production behavior are outside this analysis. Review the candidate manually; nothing is applied.

Candidate YAML uses `replace` with an empty replacement to remove a label, scoped
by anchored exact `__name__` alternatives. Unconditional `labeldrop` cannot express
this metric scope. Auto-generated `up`/`scrape_*` metrics are excluded from config
emission. Candidates belong under one reviewed scrape job; they are not complete
Prometheus configuration files. JSON has `metric_relabel_configs_yaml` when emitted.
The candidate rules have independent semantic tests; they have not been validated
by `promtool` or run against a live Prometheus instance.

Supported scrape input: UTF-8, LF-terminated Prometheus text 0.0.4; legacy
ASCII metric/label names; quoted Unicode label values; `\\`, `\"`, `\n` label
escapes; decimal float64 values, `NaN`, `Inf`, `+Inf`, `-Inf`; optional signed int64
integer millisecond timestamps; HELP/TYPE (before samples); counters, gauges,
untyped metrics, classic histogram and summary sample families. No TYPE means
an untyped exact-name family; suffixes alone never imply a histogram. Family
statistics count bucket/quantile series individually. All label values are listed.

Duplicate semantic input series fail, even if timestamps differ. Empty label values
are equivalent to absent labels for canonical identity; explicit empties remain in
statistics and witnesses. Labels are sorted structurally; decoded strings are never
concatenated with ambiguous delimiters. Collision witnesses preserve value tokens,
timestamps, original labels and line numbers. Values compare as float64; NaN yields
`indeterminate_nan`. Any collision is unsafe regardless of value or timestamp.

Rejected: `__name__`, `le`, `quantile` removal; internal `__*` input labels; absent
requested labels in the selected metric scope; malformed/duplicate metadata or
series; CRLF/NUL; non-integer/out-of-range timestamps; overflowing decimal values;
OpenMetrics UNIT/EOF, exemplars, UTF-8 name syntax, native histograms, protobuf,
TSDB or directly concatenated scrapes (use a manifest for separate captures).
Ordinary `#` comments are ignored. This is syntax and identity analysis, not full histogram monotonicity/completeness validation. Input,
statistics and collision witnesses are held in memory without truncation; large
scrapes can consume substantial memory. No arbitrary scripts, API keys, network
requests, rewriting, aggregation, production config editing or automatic application.

License and attribution: see [THIRD_PARTY.md](../THIRD_PARTY.md).
