# otelwrench privacy

A Collector config with a `redaction`, `attributes` or `transform` processor looks safe, but a valid config is not proof that every sensitive field is gone: ignored keys bypass checks, integers and arrays slip past string masks, and span names, events, links, scope metadata and log bodies sit outside the attribute traversal. `otelwrench privacy` runs the selected pipeline's local processors in an **actual, version- and SHA-256-pinned OpenTelemetry Collector binary** on synthetic telemetry you supply, records what really survives, and checks your privacy expectations against that output.

Use it in CI or on a laptop before you change a pipeline's privacy processors, upgrade the Collector, or claim that a field is scrubbed. It does not implement redaction rules or infer protection from YAML settings; the Collector does all processing.

## Quick start

`otelwrench privacy` needs the `privacy` extra (DeepDiff, python-json-pointer, jsonpath-ng and the OpenTelemetry protobuf classes) and an `otelcol-contrib` executable. The Collector binary is never bundled with otelwrench and never downloaded when otelwrench is installed.

```sh
pip install 'otelwrench[privacy]'
otelwrench privacy install-collector       # explicit download into .tools/, SHA-256 verified and pinned
otelwrench privacy run \
  --config examples/privacy/collector.yaml --pipeline traces \
  --sample examples/privacy/traces.pb --expect examples/privacy/expectations.yaml \
  --out artifacts/example
```

The samples are in [`examples/privacy/`](../examples/privacy/), so run the commands from a clone of the repository. `--collector` and `--lock` default to `.tools/otelcol-contrib` and `.tools/collector.lock.json` in the current directory.

The example deliberately leaves privacy gaps. Check a stricter expectation:

```sh
otelwrench privacy run \
  --config examples/privacy/collector.yaml --pipeline traces \
  --sample examples/privacy/traces.pb --expect examples/privacy/privacy-fails.yaml \
  --out artifacts/privacy-failure
# exits 1: ignored secrets, integer card value and non-attribute data survive
```

Exit codes: **0** completed and supplied checks passed (or observations only), **1** privacy expectations failed, **2** input/runtime/unsupported-config error, **130** interrupted. A successful run proves only the supplied expectations over the supplied sample on the recorded binary. Sentinel searches are literal; they are not a PII detector and do not decode encoded/hashed secrets.

## Commands

| Command | What it does |
| --- | --- |
| `otelwrench privacy run` | Replay one selected local processor pipeline and write evidence to a fresh `--out` directory |
| `otelwrench privacy pin` | Record an explicitly chosen binary's version and SHA-256 in a lock file |
| `otelwrench privacy install-collector` | Download the official Collector release archive, verify its SHA-256 and write the binary and lock to `.tools/` |

`run` options: `--config` (Collector YAML), `--sample` (OTLP JSON, JSONL or request protobuf), `--pipeline` (exact `service.pipelines` key, for example `traces` or `logs/pii`), `--expect` (privacy assertions; omit for observations only), `--explain-ignored`, `--collector`, `--lock`, `--out` (required, must not exist), `--format auto|json|protobuf` (default `auto`), `--timeout` (seconds per Collector stage, 0.2–60, default 15).

`pin` options: `--collector` (required), `--version` (required; the version the binary must report), `--out` (default `.tools/collector.lock.json`).

`install-collector` options: `--version` (default `0.72.0`), `--dest` (default `.tools`), `--force` (replace an existing pinned binary).

## Installing and pinning a Collector

`install-collector` is the only part of otelwrench that downloads anything, and only when you run it. It fetches the official `otelcol-contrib` archive from the OpenTelemetry Collector releases on GitHub, verifies it against the release's published checksums, and for the built-in pin also against a checksum stored in otelwrench. It refuses downloads over 50 MiB, extracts only the `otelcol-contrib` executable, and writes `.tools/collector.lock.json`.

The built-in pin is Collector **0.72.0** for macOS/arm64, archive SHA-256 `77777ea9aaa73b5ffb0cf7fef689fdf9826dca1ccaed9cf9666135b38ad06e70`. It is an old compatibility fixture chosen for the 50 MiB download limit, **not a production-version recommendation**. Linux installation may exceed the limit; supply a preinstalled binary instead. Newer versions can be chosen explicitly:

```sh
otelwrench privacy pin --collector /path/to/otelcol-contrib \
  --version 0.120.0 --out .tools/collector.lock.json
otelwrench privacy run --collector /path/to/otelcol-contrib \
  --config examples/privacy/collector.yaml --pipeline traces \
  --sample examples/privacy/traces.pb --out artifacts/observations
```

`pin` records the binary you explicitly supply. It does not authenticate an arbitrary binary's origin; use trusted upstream release checksums. A run rejects either a version or executable hash mismatch. Omitting `--expect` produces an observation report with `privacy_verdict: not_asserted`.

The installer reuses a matching local pin, checks extraction space, and preserves the previous binary/lock if promotion fails. `--force` explicitly replaces an existing installation. If installer rollback itself cannot rename a previous executable, it is retained as `.tools/.otelcol-contrib.previous` outside temporary staging. The error names that recovery path; the old lock is retained and a further replacement refuses to overwrite the recovery copy.

Replay uses POSIX process shutdown and is verified on macOS/arm64. Windows replay is outside this version's scope. Only traces, logs and metrics are accepted; profiles and compressed/framed protobuf files are not supported.

## What is executed

1. Select the exact `service.pipelines` key and preserve its processor order.
2. Check the Collector's version and SHA-256 against the lock.
3. Run a no-processor normalization pass through its OTLP receiver/file exporter.
4. Run each processor in order in a short-lived Collector subprocess, capturing real output at each stage. All processes are stopped before the run returns.
5. Execute the **whole actual pipeline on the original requests**, and require agreement with the isolated stages before attributing changes or testing privacy. A nondeterministic/batch-sensitive discrepancy is an explicit error; the actual whole-pipeline output is preserved.
6. Compare typed outputs and evaluate JSONPath expectations.

Only `redaction`, `attributes`, `resource`, `transform`, `filter`, and `batch` processor types are accepted. An unknown processor is an error, never silently skipped. Actual option/signal support is validated by the chosen Collector. In 0.72.0, redaction is trace-only; the example logs/metrics pipelines fail explicitly. Batch buffers are flushed by orderly Collector shutdown, then checked against the actual whole-pipeline result. Unknown/stateful/network components outside this list remain explicit errors.

Original receivers, exporters, extensions and other pipelines are not launched. The generated config uses an ephemeral **127.0.0.1** OTLP/HTTP receiver, local file exporter and disabled Collector metrics. No production endpoint is contacted. The Collector process does not inherit `OTEL_*` or other environment variables. Selected processor configs must be self-contained; `${...}` provider/environment references are rejected. This is processor replay, not full deployment validation.

## Input and evidence formats

Input is OTLP JSON, file-exporter JSONL, or an `Export*ServiceRequest` protobuf (`.pb`, `.bin`, `.protobuf`, or `--format protobuf`). Signal comes from the pipeline. The upstream Collector handles OTLP parsing, AnyValue types, IDs and serialization. Do not substitute generic protobuf-JSON trace IDs: OTLP JSON uses hexadecimal IDs.

The supplied binary fixtures come from the real OpenTelemetry Python SDK, not hand-assembled payloads; see [fixture provenance](../examples/privacy/PROVENANCE.md).

A fresh `--out` directory contains:

- `input.json`, `output.json`: actual Collector OTLP JSON, aggregated from its JSON-lines file exporter without decoding debug text.
- `input.view.json`, `output.view.json`: readable copies with `attributes` and structured-body key/value lists indexed by key. AnyValue wrappers are retained, so strings and integer values remain distinct. Duplicate keys cause an explicit ambiguity error rather than disappearing.
- `stages/`: bounded process logs, actual generated Collector configs and raw exporter output for each replay step.
- `report.json`: runtime identity, checks, every leaf location's before/after status, retained non-attribute fields, actual summary attributes, and separately labelled ignored-key observations. The ingestion audit separately lists known source fields lost **before processors**, and flags unknown protobuf wire fields. It uses official generated protobuf classes; it has a 1 MiB/30,000-node budget and reports when unavailable. Errors retain a report and partial evidence.

Attribute list reordering does not fabricate diffs. Records and other arrays use positional alignment; filtering/reordering can make individual record attribution ambiguous. The full raw outputs remain available. The inventory compares **leaf positions**: expanding an empty object/list is represented as removal of the empty leaf plus addition of its children, not a claim that the containing field vanished. Use the full typed output or a pointer expectation for container existence.

The type-coverage section lists actual input/output locations for all seven OTLP `AnyValue` types, unchanged locations, and unexercised input types. It is a sample coverage inventory, not a static claim about which rules support which types. Input hashes are taken from the consumed byte snapshots. The report also records dependency versions and output hashes, and rejects a binary changed during replay. Python version, operating system and architecture are recorded without a host name or user-directory path.

## Expectations

Checks address the **view** using [jsonpath-ng](https://github.com/h2non/jsonpath-ng). Use brackets for attribute names containing punctuation:

```yaml
forbid: [SYNTHETIC-SECRET]  # literal search across output values AND keys
checks:
  - name: a span survives
    path: $.resourceSpans[*].scopeSpans[*].spans[*]
    op: count
    value: 1
  - name: email is removed
    path: $.resourceSpans[*].scopeSpans[*].spans[*].attributes['customer.email']
    op: absent
  - name: control is unchanged
    path: $.resourceSpans[*].scopeSpans[*].spans[*].attributes.safe
    op: unchanged
```

Supported operations: `absent`, `present`, `equals`, `not_contains`, `count`, `changed`, `unchanged`. `equals`, `not_contains`, `count` require `value`. `absent` requires an actual input match, catching path typos. `present`, `equals` and `not_contains` require at least one output match. `equals` applies to every match with types preserved. `changed` requires every selected input location to change or disappear. `count` counts JSONPath matches, so use `[*]` for records. Pair privacy assertions with survivor-count and untouched-control checks.

`forbid` also requires its literal sentinel to occur in the normalized input; otherwise it fails as unexercised coverage. Use a root `not_contains` check when you intentionally want an output-only absence invariant.

Every reported location also has an RFC 6901 `pointer`, generated and resolved with [python-json-pointer](https://github.com/stefankoegl/python-json-pointer). Use `pointer: /resourceSpans/0/resource/attributes/owner.email` instead of `path` for exact addresses, particularly keys containing control characters. Supply exactly one selector. An empty pointer selects the root. The JSONPath parser cannot round-trip newline-containing field names; pointers retain exact names.

JSONPath is restricted to actual field/array selections and simple literal comparison filters. Arithmetic, regex filters, transformation operators, unions, repeated root resets and multiple recursive descents are rejected. Paths are limited to 2,048 characters and 128 syntax nodes. Numeric comparisons over incompatible data types fail explicitly rather than becoming a passing check.

## Other real upstream integrations

`examples/privacy/collector-transform.yaml` composes the existing OTTL transform processor, including resource/scope attributes, both log-body formats, metric metadata and datapoints. These examples preserve counts and a safe control and pass on 0.72.0:

```sh
otelwrench privacy run --config examples/privacy/collector-transform.yaml \
  --sample examples/privacy/logs.pb --pipeline logs --expect examples/privacy/logs-expectations.yaml \
  --out artifacts/logs
otelwrench privacy run --config examples/privacy/collector-transform.yaml \
  --sample examples/privacy/metrics.pb --pipeline metrics --expect examples/privacy/metrics-expectations.yaml \
  --out artifacts/metrics
```

The trace transform example still retains link attributes. Likewise, `examples/privacy/collector-noop-links.yaml` is accepted by 0.72.0 but `set(links, [])` does not remove the links. These are reproducible observations on that pin; a config the Collector accepts does not prove the requested mutation happened.

```sh
otelwrench privacy run --config examples/privacy/collector-noop-links.yaml \
  --sample examples/privacy/traces.pb --pipeline traces --expect examples/privacy/links-must-be-removed.yaml \
  --out artifacts/link-noop
# exits 1 on 0.72.0: the span/control survive but its link was not removed
```

## Rule hits and uncovered data

The tool preserves your `summary` setting. It reports **actual upstream summary attributes**, including whether they were already present on input. Incoming `redaction.*` values can be spoofed, so these are diagnostics, not an independent proof of which regex ran. The Collector does not expose a per-pattern rule trace. With `summary: silent`, value changes remain visible; diagnostic hits may not be.

Add `--explain-ignored` to run a separate counterfactual pass for every redaction stage with nonempty `ignored_keys` or `ignored_key_patterns`. The adapter clears those settings as a group and executes the **real same pinned processor** on the same stage input. The nested `ignored_rule_counterfactual` shows the resulting location changes. It does not replace the configured pipeline's output or change its privacy verdict. This can explain an observed bypass even with silent summaries; it is an experiment over modified config, not an internal rule trace. Summary-namespace changes are retained in the complete diff, but separated from `non_diagnostic_affected_locations`: a changed ignored-count alone does not mean telemetry data changed. Primary leak checks still inspect every output field, including diagnostic attributes.

An ignored key plus unchanged output is explicitly a configuration observation, not invented engine instrumentation. Event/link attributes, scope metadata, log bodies, status messages and metric names must be checked in the real output. The report lists untested signals; an unchanged location is evidence of survival for that fixture, not a universal assertion that every Collector version ignores it.

Collector 0.71.0 rejects the `ignored_keys` option at startup; without that option, the same allowlist removes the secret and the card is masked.

## Limits

The built-in limits are 16 MiB input, 32 MiB output per stage, 1000 requests, 8 processors, 100 expectations, and a configurable 0.2–60 second timeout per stage. Evidence analysis also has a 200,000-node/depth-64 budget, checked before views, diffs or queries are created; a 5,000-span sample exceeds it. Oversized samples return an error and retain the raw Collector export; split them into smaller runs.

Collector output logs are drained with a 1 MiB capture cap and truncation metadata. Startup waits for that process's standard readiness message before connecting, so a port collision cannot send samples to an unrelated listener. A custom distribution without the upstream readiness message fails explicitly at startup. SIGTERM/SIGINT stop and reap the child; atomic report writes retain previous evidence if a storage error interrupts a write.

## Tests

Tests under `tests/privacy/` that need a real Collector are marked `integration` and skip unless `.tools/otelcol-contrib` (version 0.72.0) exists at the repository root; install it with `otelwrench privacy install-collector` from the repository root. They run serially with short-lived real Collector processes.

## Prior art

Existing tools already cover configuration validation and replay; this command is an integration adapter around the unmodified Collector, not another redaction engine. See [privacy/prior-art.md](privacy/prior-art.md) for the surveyed tools (Collector testbed and telemetrygen, OTelBin, base14 Scout `config test`, Telflo pipeline tests) and the narrower gap this command fills: a local, account-free privacy expectation check over actual output, including fields outside attributes. Attribution for the libraries it uses is in [THIRD_PARTY.md](../THIRD_PARTY.md#otelwrench-privacy-formerly-otel-privacy-replay).
