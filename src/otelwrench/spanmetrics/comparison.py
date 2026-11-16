# SPDX-License-Identifier: Apache-2.0
"""Capture-local dimension comparison, with exact joint attribute attribution."""
from __future__ import annotations

from collections import Counter

from . import __version__
from .core import BASE_DIMENSIONS, Config, InputError, _convert, decimal_text


VALIDATION_FIELDS = ('status', 'input_records', 'unique_records', 'duplicate_records',
                     'rejected_records', 'record_counts_complete', 'diagnostics', 'diagnostics_omitted')


def comparison_report(baseline: Config, candidate: Config, reports: list[dict]) -> dict:
    """Start with unavailable counts; publish counts only after both validations pass."""
    complete = all(r['status'] == 'ok' and r['record_counts_complete'] for r in reports)
    return {
        'schema_version': 1, 'tool_version': __version__, 'report_type': 'dimension_comparison',
        'status': 'ok' if complete else 'rejected', 'comparison_complete': complete,
        'scope': 'Exact counts of validated unique spans in this capture only; no production traffic or cost extrapolation.',
        'metrics_emitted': False,
        'input_format': reports[0]['input_format'],
        'histogram_bounds_seconds': [decimal_text(b) for b in baseline.buckets],
        'series_per_group': baseline.series_per_group,
        'limits': {k: getattr(baseline, k) for k in ('max_series', 'max_spans', 'max_input_bytes', 'max_line_bytes')},
        'custom_dimension_encoding': reports[0]['custom_dimension_encoding'],
        'duplicate_policy': reports[0]['duplicate_policy'],
        'unique_records': reports[0]['unique_records'] if complete else None,
        'series_delta': reports[1]['series'] - reports[0]['series'] if complete else None,
        'configurations': {
            name: {'dimensions': r['dimensions'], 'groups': r['groups'] if complete else None,
                   'series': r['series'] if complete else None,
                   'validation': {key: r[key] for key in VALIDATION_FIELDS}}
            for name, r in zip(('baseline', 'candidate'), reports)
        },
        'attribution': 'Partitions share mandatory labels and common source/key dimensions, regardless of label renames. '
                       'Joint observed source combinations map to full identities in each configuration; '
                       'series_delta is candidate minus baseline. No per-attribute causal estimate is made.',
        'partitions': [],
    }


def compare(data: bytes, baseline: Config, candidate: Config) -> dict:
    """Compare dimension selections on identical bytes with identical bounds and limits."""
    for key in ('buckets', 'max_series', 'max_spans', 'max_input_bytes', 'max_line_bytes', 'input_format'):
        if getattr(baseline, key) != getattr(candidate, key):
            raise InputError(f'comparison configurations must share {key}')
    accepted = [{}, {}]
    reports = [_convert(data, config, labels).report
               for config, labels in zip((baseline, candidate), accepted)]
    result = comparison_report(baseline, candidate, reports)
    result['input_sha256'] = reports[0]['input_sha256']
    if not result['comparison_complete']:
        return result

    # Tuples and structured JSON identities avoid delimiter-based collisions.
    # Identity sets match because both independent full-capture validations passed.
    base_names = [d['label'] for d in BASE_DIMENSIONS]
    names = [base_names + [d.label for d in config.dimensions] for config in (baseline, candidate)]
    indexes = [{(d.source, d.key): i for i, d in enumerate(config.dimensions, len(base_names))}
               for config in (baseline, candidate)]
    sources = sorted(indexes[0].keys() | indexes[1].keys())
    shared = sorted(indexes[0].keys() & indexes[1].keys())

    def source_values(keys, labels):
        return tuple(labels[0][indexes[0][key]] if key in indexes[0] else labels[1][indexes[1][key]]
                     for key in keys)

    def attributes(keys, values):
        return [{'source': source, 'key': key, 'value': value}
                for (source, key), value in zip(keys, values)]

    partitions = {}
    for identity, baseline_labels in accepted[0].items():
        labels = (baseline_labels, accepted[1][identity])
        key = (baseline_labels[:len(base_names)], source_values(shared, labels))
        partitions.setdefault(key, Counter())[labels] += 1
    for (mandatory, shared_values), combinations in sorted(partitions.items()):
        baseline_groups = len({labels[0] for labels in combinations})
        candidate_groups = len({labels[1] for labels in combinations})
        result['partitions'].append({
            'mandatory_labels': dict(zip(base_names, mandatory)),
            'shared_attributes': attributes(shared, shared_values),
            'baseline_groups': baseline_groups, 'candidate_groups': candidate_groups,
            'series_delta': (candidate_groups - baseline_groups) * baseline.series_per_group,
            'attribute_combinations': [
                {'source_attributes': attributes(sources, source_values(sources, labels)),
                 'baseline_labels': dict(zip(names[0], labels[0])),
                 'candidate_labels': dict(zip(names[1], labels[1])), 'unique_records': count}
                for labels, count in sorted(combinations.items())
            ],
        })
    return result
