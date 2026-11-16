"""Replay and compare policy sets on one finite, complete trace corpus."""

from .fingerprints import replay_fingerprints

BYTE_MEASURE = 'compact sorted-key UTF-8 OTLP JSON request; no newline; original grouping, empty groups removed'


def report_assumptions():
    """Keep interpretation limits attached to saved replay/comparison reports."""
    return {
        'corpus_completeness': 'Caller must supply all spans of every trace; completeness is not verified.',
        'evaluation': 'All captured spans per trace are evaluated together using final complete-trace duration.',
        'online_timing_or_cache_equivalence': False,
        'production_savings_estimated': False,
        'byte_scope': 'Canonical serialized JSON for this finite corpus only; not transport, storage, or cost.'}


def replay(corpus, policies):
    rows, kept = [], set()
    for tid, trace in corpus.traces.items():
        votes = [policy.evaluate(trace) for policy in policies]
        drop = any(v['decision'] == 'dropped' for v in votes)
        sample = not drop and any(v['decision'] == 'sampled' for v in votes)
        if sample:
            kept.add(tid)
        rows.append({'trace_id': tid, 'duration_ns': trace.duration_ns,
                     'duration_ms_floor': trace.duration_ns // 1_000_000,
                     'error': trace.error, 'statuses': sorted(trace.statuses),
                     'span_count': trace.span_count,
                     'kept_span_count': trace.span_count if sample else 0,
                     'decision': 'sample' if sample else 'drop',
                     'reason': 'drop_policy_precedence' if drop else ('sample_policy_match' if sample else 'no_sample_policy_match'),
                     'matched_policy_names': [v['name'] for v in votes if v['matched']],
                     'policies': votes})
    errors = sum(t.error for t in corpus.traces.values())
    kept_errors = sum(corpus.traces[tid].error for tid in kept)
    total_bytes = corpus.retained_bytes(set(corpus.traces))
    kept_bytes = corpus.retained_bytes(kept)
    return {'schema_version': 1, 'mode': 'complete_trace_offline',
            'fingerprints': replay_fingerprints(corpus, policies),
            'assumptions': report_assumptions(),
            'byte_measure': BYTE_MEASURE,
            'summary': {'total_traces': len(rows), 'kept_traces': len(kept),
                        'dropped_traces': len(rows) - len(kept),
                        'total_spans': sum(t.span_count for t in corpus.traces.values()),
                        'kept_spans': sum(corpus.traces[tid].span_count for tid in kept),
                        'error_retention': {'total': errors, 'kept': kept_errors,
                                            'ratio': kept_errors / errors if errors else None},
                        'source_file_bytes': corpus.source_bytes,
                        'serialized_total_bytes': total_bytes,
                        'serialized_kept_bytes': kept_bytes,
                        'serialized_removed_bytes': total_bytes - kept_bytes},
            'traces': rows}


def sampled_ids(report):
    return {t['trace_id'] for t in report['traces'] if t['decision'] == 'sample'}


def compare(corpus, baseline, candidate):
    before, after = replay(corpus, baseline), replay(corpus, candidate)
    changed = [{'trace_id': a['trace_id'], 'baseline': a['decision'],
                'candidate': b['decision'], 'error': a['error'],
                'span_count': a['span_count'],
                'baseline_matched_policy_names': a['matched_policy_names'],
                'candidate_matched_policy_names': b['matched_policy_names'],
                'baseline_reason': a['reason'], 'candidate_reason': b['reason'],
                # Include nonmatches too: they explain no-match drops and NOT
                # children. Reuse the evaluated trees; never rerun policies here.
                'baseline_policies': a['policies'], 'candidate_policies': b['policies']}
               for a, b in zip(before['traces'], after['traces']) if a['decision'] != b['decision']]
    bs, cs = before['summary'], after['summary']
    return {'schema_version': 1, 'mode': 'complete_trace_offline_comparison',
            'fingerprints': {'version': 1, 'algorithm': 'sha256',
                             'corpus_source_sha256': corpus.source_sha256,
                             'baseline_effective_config_sha256': before['fingerprints']['effective_config_sha256'],
                             'candidate_effective_config_sha256': after['fingerprints']['effective_config_sha256']},
            'assumptions': report_assumptions(),
            'byte_measure': BYTE_MEASURE, 'baseline': before, 'candidate': after,
            'comparison': {'changed_trace_count': len(changed), 'changed_traces': changed,
                           'newly_kept_traces': len(sampled_ids(after) - sampled_ids(before)),
                           'newly_dropped_traces': len(sampled_ids(before) - sampled_ids(after)),
                           'error_traces_lost': sorted(t for t in sampled_ids(before) - sampled_ids(after) if corpus.traces[t].error),
                           'error_traces_gained': sorted(t for t in sampled_ids(after) - sampled_ids(before) if corpus.traces[t].error),
                           'kept_span_delta': cs['kept_spans'] - bs['kept_spans'],
                           'serialized_kept_byte_delta': cs['serialized_kept_bytes'] - bs['serialized_kept_bytes']}}
