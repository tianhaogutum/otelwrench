# otelwrench tracepack

Move a saved trace between OTLP JSON, Jaeger UI exports and Zipkin v2 JSON without
silently losing anything. `otelwrench tracepack` converts one saved trace document,
writes the converted trace to stdout or a file, and writes a separate loss report that
names the exact source path of every field it could not carry over. Use it when you
attach traces to bug reports or test fixtures, move captures between backends, or need
a reproducible fixture: `bundle` packs the original bytes, the canonical OTLP
normalization and the loss report into one hash-checked file that `verify` replays
offline and `extract` restores byte for byte.

Everything runs locally on files or stdin. There are no network calls, collectors,
services, generated trace IDs or third-party dependencies.

## Quick start

```sh
pip install otelwrench
git clone https://github.com/tianhaogutum/otelwrench && cd otelwrench

otelwrench tracepack convert examples/tracepack/zipkin.json --from zipkin --to otlp --output out.json
```

`out.json` keeps the trace ID `11111111111111111111111111111111`, span ID
`2222222222222222`, operation `GET /` and resource `service.name=web`, with exact
nanosecond times:

```json
{
  "startTimeUnixNano": "1700000000000000000",
  "endTimeUnixNano": "1700000000001200000"
}
```

Stderr carries the report (pretty-printed in real output):

```json
{"from":"zipkin","to":"otlp","spanCount":1,"losses":[]}
```

More examples, all with synthetic data:

```sh
# Auto-detect a two-service Jaeger export; the cross-service parent is preserved.
otelwrench tracepack convert examples/tracepack/jaeger.json --to otlp --strict-loss > /tmp/tracepack-otlp.json

# OTLP resource attributes become Jaeger processes and process tags.
otelwrench tracepack convert examples/tracepack/otlp.json --to jaeger --strict-loss > /tmp/tracepack-jaeger.json

# OTLP -> Zipkin -> OTLP, loss-free in both directions for this input.
otelwrench tracepack convert examples/tracepack/otlp.json --to zipkin --strict-loss --output /tmp/tracepack-zipkin.json
otelwrench tracepack convert /tmp/tracepack-zipkin.json --to otlp --strict-loss

# One document on stdin; stdout stays valid trace JSON, stderr gets the report.
otelwrench tracepack convert - --to otlp < examples/tracepack/zipkin.json > /tmp/out.json 2> /tmp/loss.json

# Report unsupported traceState and sub-microsecond truncation; write nothing.
otelwrench tracepack convert examples/tracepack/otlp-lossy.json --to jaeger --dry-run

# Exits 2: the same three losses are rejected before any output.
otelwrench tracepack convert examples/tracepack/otlp-lossy.json --to jaeger --strict-loss --dry-run
```

## Command line

```text
otelwrench tracepack convert INPUT|- --to otlp|jaeger|zipkin
    [--from auto|otlp|jaeger|zipkin]
    [--output FILE|-] [--strict-loss] [--dry-run] [--zipkin-error-tag]
otelwrench tracepack bundle INPUT|- [--from auto|otlp|jaeger|zipkin]
    [--output FILE|-] [--strict-loss]
otelwrench tracepack verify BUNDLE|- [--source SOURCE] [--strict-loss] [--report]
otelwrench tracepack extract BUNDLE|- --output FILE [--strict-loss]
otelwrench tracepack --help
```

| Flag | Commands | Meaning |
| --- | --- | --- |
| `--from` | convert, bundle | Input format; `auto` (default) detects exactly one of `resourceSpans`, Jaeger `{data:[...]}` or a Zipkin array |
| `--to` | convert | Required target format |
| `--output FILE\|-` | convert, bundle, extract | Atomic file output, or `-` for stdout (default; extract requires a file) |
| `--strict-loss` | all | Exit 2 instead of succeeding when the report has any loss |
| `--dry-run` | convert | Parse, validate and convert fully, print the report, write nothing |
| `--zipkin-error-tag` | convert | OTLP/Jaeger to Zipkin only: add `error: ""` to carry ERROR status |
| `--source SOURCE` | verify | Also require a local regular file to equal the embedded original bytes |
| `--report` | verify | Append the replay-verified loss report to the success JSON |

Flags take no `=value` form and may not repeat. Input is one UTF-8 JSON document of at
most 64 MiB and 128 nesting levels (a leading UTF-8 BOM is accepted). NDJSON,
protobuf, gzip, Zipkin nested arrays and UI-enriched envelopes are not supported.

### Exit codes and output channels

| Code | Meaning |
| --- | --- |
| 0 | Success; inspect `losses`, or use `--strict-loss` |
| 1 | Malformed or unsupported input, invalid flags, verification failure or file I/O error |
| 2 | `--strict-loss` refused one or more losses |

Every error is exactly one JSON object on stderr with an `error` member; strict-loss
refusals and I/O errors after a conversion also include `report`. Validation and
strict-loss failures write no trace bytes. Each `losses` entry has `path` (JSONPath
style, rooted at `$`) and `reason`; entries are sorted by path and reason and never
contain attribute values, though they can name source fields.

Argument errors are checked before any file or stdin access, in a fixed order:
syntactic errors (duplicate, unknown or foreign flags, missing values) in argument
order, then an invalid `--from`, then a missing or invalid `--to`, then a
`--zipkin-error-tag` combination that cannot work (non-Zipkin `--to` or
`--from zipkin`). For example `convert - --to bad --from bad` reports the `--from`
error without waiting for stdin.

### Inputs and outputs

INPUT and BUNDLE paths must open to a regular file (symlinks to regular files are
fine). Paths are opened non-blocking and the opened handle is checked, so directories,
devices and FIFOs are refused promptly with
`{"error":"input must be a regular file, not a directory/device/FIFO"}` (`bundle input
...` for verify/extract). Use explicit `-` to read stdin, including a pipe or FIFO you
redirect yourself; reads are byte-counted against the 64 MiB (source) or 256 MiB
(bundle) limit.

File output goes to an exclusive mode-0600 temporary file in the destination
directory, is flushed, then atomically renamed over the destination. The parent must
exist; symlinks, directories and devices are refused. An existing file ends up with
mode 0600. Input and output may be the same path. Stdout has no atomicity guarantee.

## Reusable trace fixtures

```sh
# Bundle any supported trace; OTLP is the fixed normalization target.
otelwrench tracepack bundle examples/tracepack/jaeger.json --strict-loss --output /tmp/jaeger.tracepack.json
otelwrench tracepack verify /tmp/jaeger.tracepack.json

# The shipped bundle verifies without a separate source file.
otelwrench tracepack verify examples/tracepack/zipkin.tracepack.json
otelwrench tracepack verify examples/tracepack/zipkin.tracepack.json --report
otelwrench tracepack verify examples/tracepack/zipkin.tracepack.json --strict-loss
otelwrench tracepack verify examples/tracepack/zipkin.tracepack.json --source examples/tracepack/zipkin.json
otelwrench tracepack verify - --source examples/tracepack/zipkin.json < examples/tracepack/zipkin.tracepack.json

# Verify fully, then atomically restore the exact original bytes.
otelwrench tracepack extract examples/tracepack/zipkin.tracepack.json --output /tmp/restored-zipkin.json
cmp examples/tracepack/zipkin.json /tmp/restored-zipkin.json

# Bundle stdout can be piped straight into the verifier.
otelwrench tracepack bundle examples/tracepack/zipkin.json | otelwrench tracepack verify -
```

Verification prints a summary such as
`{"valid": true, "format": "tracepack-fixture", "version": 1, "sourceSha256": "...",
"spanCount": 1, "lossCount": 0, "guarantee": "reproducibility-only"}`; `--source` adds
`"sourceMatches": true` and `--report` adds the complete `report` last. The shipped
example's source SHA-256 is
`6efe057f75ff6c425c88f7a7a6075909a8591040c44d4fb62914bdbf6f4a940c`.

### Bundle format (`tracepack-fixture` v1)

The format is unchanged from the original standalone tool, so existing bundles keep
verifying. A bundle is one UTF-8 JSON document with exactly these members:

| Member | Content |
| --- | --- |
| `format`, `version` | `"tracepack-fixture"`, integer `1` |
| `converter` | `name: "tracepack"`, `version: "0.1.0"`, `normalizationVersion: 1` (format identifiers, not the otelwrench version) |
| `source` | `encoding: "base64"`, `data`: canonical padded base64 of the exact original bytes, including whitespace and any BOM |
| `options` | Effective `from` (`otlp`, `jaeger` or `zipkin`), fixed `to: "otlp"`, boolean `strictLoss` |
| `normalized` | Canonical OTLP output, with decimal-string nanoseconds/int64 values and normalized IDs |
| `lossReport` | `{from,to,spanCount,losses}`, with an empty `losses` array when loss-free |
| `sha256` | Lowercase hex SHA-256 of `source` (raw bytes) and of the canonical JSON of `options`, `normalized`, `lossReport` |

Canonical JSON v1 sorts object keys by UTF-16 code units, keeps array order, emits no
whitespace and uses JavaScript number formatting with exact integers and `-0`
preserved. It is a tracepack-specific contract, not RFC 8785/JCS. Bundle files are
that serialization plus one newline, so the same bytes and options always produce
identical bundle bytes.

`verify` rejects unknown or missing members, non-canonical base64, other
format/converter versions and wrong digests, then re-runs the recorded normalization
on the embedded bytes and compares both `normalized` and `lossReport`. A recomputed
hash cannot hide an edited member: the error is
`bundle.normalized: does not match offline conversion replay` (or `bundle.lossReport`).
`--source` comparison happens only after that, and compares raw bytes: BOM removal,
CRLF changes, whitespace or re-spelled numbers all mismatch. `--source -` is refused;
use `./-` for a file named `-`. `--strict-loss` applies after verification and the
optional source check.

`extract` runs the same full verification before touching the destination, then
writes the decoded original bytes with the atomic writer. With `--strict-loss`, a
bundle whose verified normalization has losses exits 2 without touching the
destination.

This verifies **reproducibility, not authenticity**: anyone can regenerate a
consistent bundle with matching hashes. There are no signatures or trust anchors.

## Supported format subset

| Feature | OTLP JSON | Jaeger export | Zipkin v2 JSON |
| --- | --- | --- | --- |
| Envelope | `{resourceSpans:[...]}` with `scopeSpans[].spans[]` | `{data:[{traceID,spans,processes}]}` | Flat array of spans |
| Time | `startTimeUnixNano`, `endTimeUnixNano`, event `timeUnixNano` | `startTime`, `duration`, log `timestamp` (µs) | `timestamp`, `duration`, annotation `timestamp` (µs) |
| Parent | `parentSpanId` | Zero or one same-trace `CHILD_OF` reference | `parentId` |
| Service | String resource attribute `service.name` | Process `serviceName` | `localEndpoint.serviceName` |
| Span tags | `stringValue`, `boolValue`, `intValue`, `doubleValue` | `string`, `bool`, `int64`, `float64` | String tags only |
| Events | Name, time, primitive attributes | Logs; string `event` field is the name | Annotation `value` is the name |
| Kind | Integers 0–5 | `span.kind` tag | `SERVER`, `CLIENT`, `PRODUCER`, `CONSUMER` |
| Status | Code 0–2 and message | `otel.status_code` / `otel.status_description` (or bool `error=true`) | String `error` tag implies ERROR |

Unsupported metadata is reported, not dropped silently: OTLP scopes, schema URLs,
`traceState`, flags, links, nested/array/bytes attributes and nonzero dropped counts;
Jaeger warnings, query metadata and unused processes; Zipkin debug flags, remote
endpoints and endpoint addresses. Known fields with invalid values fail instead of
being coerced. Duplicate JSON keys and duplicate attribute keys fail. Identical
duplicate spans collapse with a loss entry; conflicting duplicates fail.

Zipkin output keeps string tags only and drops other resource attributes, nonstring
tags and event attributes, each with a loss at its source path. Zero or
sub-microsecond durations round up to 1 µs with a loss. Status other than what an
`error` tag implies, and any status message, is reported. With `--zipkin-error-tag`,
an ERROR span without an `error` attribute gains `error: ""` (reported, because
re-import adds that attribute); a nonstring `error` attribute on an ERROR span is an
error at its source path.

```sh
otelwrench tracepack convert examples/tracepack/otlp-error.json --to zipkin --zipkin-error-tag
otelwrench tracepack convert examples/tracepack/jaeger-error.json --to zipkin --zipkin-error-tag
```

## Precision and identity rules

- Times use exact integers. OTLP nanoseconds must fit unsigned 64 bits. Unquoted OTLP
  nanoseconds beyond 2^53−1 fail; quote them (`"1700000000000000001"`).
- Jaeger/Zipkin microseconds accept integer numbers or decimal strings, even beyond
  2^53−1. Decimal/exponent tokens are accepted only when mathematically integral
  (`9.007199254740993e15` stays exact); fractional times never round into integers.
- Jaeger and Zipkin output write exact numeric microsecond tokens, including values
  outside the JavaScript safe-integer range. Readers that parse JSON numbers as
  doubles may round them.
- Conversion to microseconds floors start, duration and each event time separately;
  every nonzero remainder gets its own loss entry.
- OTLP trace IDs need 32 hex digits; Jaeger/Zipkin accept 16 or 32 and 16-digit IDs
  are left-padded with zeros. Span IDs need 16 digits. Hex is lowercased. Zero IDs
  fail.
- Missing parents are allowed; self-parenting and parent cycles fail.
- Output is deterministic: resources, processes and attributes are sorted, traces by
  ID, spans by trace/start/ID, events by time/name/attributes.

## Python API

```python
from otelwrench.tracepack import convert, create_bundle, verify_bundle, extract_bundle, canonical_json

result = convert(text, {"to": "jaeger", "strictLoss": True})   # {"output": ..., "report": ...}
bundle = create_bundle(b"[]", {"from": "zipkin", "strictLoss": True})
wire = canonical_json(bundle) + "\n"
verify_bundle(wire, {"source": b"[]", "report": True})          # summary dict
extract_bundle(wire, {"strictLoss": True})                      # b"[]"
```

Options use the original camelCase keys (`from`, `to`, `strictLoss`,
`zipkinErrorTag`, `source`, `report`); snake_case keywords (`from_=`, `strict_loss=`,
`zipkin_error_tag=`) work too. Failures raise `TracepackError`; strict-loss refusals
raise its subclass `StrictLossError` with `code == "STRICT_LOSS"` and the complete
`report`. `verify_bundle` and `extract_bundle` inspect their options only after full
verification, and `source` must be bytes-like.
