"""Finite synthetic first-arrival timers, without Collector runtime equivalence.

Timers are independent per trace: grouping and sorting arrivals gives the same
snapshots as advancing a global clock. Only finalized snapshots are evaluated;
there is no early evaluation, cache, reopening, eviction, or EOF flush.
"""

from pathlib import Path

from .corpus import identifier, parse_corpus
from .fingerprints import arrival_fingerprints
from .replay import replay
from .validation import InputError, array, canonical_bytes, integer, obj, parse_json

UINT64_MAX = 2**64 - 1


def load_arrivals(path):
    """Read JSON strictly; arrival replay/comparison validates corpus references."""
    return parse_json(Path(path).read_bytes().decode('utf-8'))


def replay_arrivals(corpus, policies, schedule, decision_wait_ms):
    """Evaluate arrivals strictly before each deadline, up to a finite cutoff.

    The reference corpus is the expected-span inventory, not proof of capture
    completeness. Schedule times are arrival times, independent of OTLP span
    timestamps. All input is validated before any report can be returned.
    """
    wait = _wait_ns(decision_wait_ms, 'decision_wait_ms')
    expected, arrivals, cutoff = _validate_arrivals(corpus, schedule)
    return _replay_arrivals(corpus, policies, expected, arrivals, cutoff, wait)


def _wait_ns(value, name):
    return integer(value, name, minimum=1, maximum=UINT64_MAX // 1_000_000) * 1_000_000


def _validate_arrivals(corpus, schedule):
    """Resolve a schedule against the reference inventory once, before replay."""
    obj(schedule, 'arrival schedule', {'schema_version', 'observation_end_unix_nano', 'arrivals'},
        ('schema_version', 'observation_end_unix_nano', 'arrivals'))
    integer(schedule['schema_version'], 'schema_version', minimum=1, maximum=1)
    cutoff = integer(schedule['observation_end_unix_nano'], 'observation_end_unix_nano', strings=True)
    expected = {tid: set() for tid in corpus.traces}
    for resource in corpus.document['resourceSpans']:
        for scope in resource.get('scopeSpans', []):
            for span in scope.get('spans', []):
                expected[span['traceId'].lower()].add(span['spanId'].lower())

    arrivals = {tid: {} for tid in expected}
    for i, item in enumerate(array(schedule['arrivals'], 'arrivals')):
        path = f'arrivals[{i}]'
        obj(item, path, {'at_unix_nano', 'trace_id', 'span_id'}, ('at_unix_nano', 'trace_id', 'span_id'))
        tid = identifier(item['trace_id'], path + '.trace_id', 32)
        sid = identifier(item['span_id'], path + '.span_id', 16)
        at = integer(item['at_unix_nano'], path + '.at_unix_nano', strings=True)
        if tid not in expected or sid not in expected[tid]:
            raise InputError(f'{path}: unknown reference span ({tid}, {sid})')
        if sid in arrivals[tid]:
            raise InputError(f'{path}: duplicate arrival for ({tid}, {sid})')
        if at > cutoff:
            raise InputError(f'{path}: arrival exceeds observation_end_unix_nano')
        arrivals[tid][sid] = at

    return expected, arrivals, cutoff


def _replay_arrivals(corpus, policies, expected, arrivals, cutoff, wait):
    rows, snapshot_ids = [], set()
    for tid, span_ids in expected.items():
        times = arrivals[tid]
        first = min(times.values()) if times else None
        deadline = first + wait if first is not None else None
        if deadline is not None and deadline > UINT64_MAX:
            raise InputError(f'trace {tid}: decision deadline exceeds unsigned 64-bit nanoseconds')
        state = 'unobserved' if first is None else ('decided' if deadline <= cutoff else 'pending')
        on_time = {sid for sid, at in times.items() if at < deadline}
        late = set(times) - on_time
        if state == 'decided':
            snapshot_ids.update((tid, sid) for sid in on_time)
        rows.append({'trace_id': tid, 'state': state,
                     'first_arrival_unix_nano': first, 'deadline_unix_nano': deadline,
                     'on_time_span_ids': sorted(on_time), 'late_span_ids': sorted(late),
                     'missing_span_ids': sorted(span_ids - times.keys()),
                     'unseen_at_decision_span_ids': sorted(span_ids - on_time) if state == 'decided' else None})

    # Reuse strict OTLP aggregation and all seven policy types on precisely the
    # deadline subsets, retaining each arriving span's resource/scope context.
    subset = corpus.retained_document(set(expected), span_ids=snapshot_ids)
    snapshot_corpus = parse_corpus(canonical_bytes(subset).decode('utf-8'))
    snapshots = {row['trace_id']: row for row in replay(snapshot_corpus, policies)['traces']}
    full = {row['trace_id']: row for row in replay(corpus, policies)['traces']}
    for row in rows:
        tid = row['trace_id']
        snapshot = snapshots.get(tid)
        row['decision_snapshot'] = snapshot
        row['full_trace'] = full[tid]
        row['decision_differs_from_full_trace'] = (
            snapshot['decision'] != full[tid]['decision'] if snapshot else None)

    return {'schema_version': 1, 'mode': 'arrival_order_offline',
            'fingerprints': arrival_fingerprints(corpus, policies, arrivals, cutoff, wait // 1_000_000),
            'model': {'name': 'first_arrival_fixed_wait_v1', 'decision_wait_ms': wait // 1_000_000,
                      'observation_end_unix_nano': cutoff,
                      'deadline_boundary': 'Only arrivals strictly before the deadline participate; timers at cutoff fire.',
                      'late_span_handling': 'Discard every late span, including spans of sampled traces; never reopen.',
                      'end_of_input': 'Do not flush pending traces or decide unobserved traces.'},
            'assumptions': arrival_assumptions(),
            'summary': {
                'reference_traces': len(rows), 'reference_spans': sum(len(s) for s in expected.values()),
                'arrived_spans': sum(len(t) for t in arrivals.values()),
                'late_spans': sum(len(r['late_span_ids']) for r in rows),
                'missing_spans': sum(len(r['missing_span_ids']) for r in rows),
                'decided_traces': len(snapshots),
                'pending_traces': sum(r['state'] == 'pending' for r in rows),
                'unobserved_traces': sum(r['state'] == 'unobserved' for r in rows),
                'incomplete_decisions': sum(bool(r['unseen_at_decision_span_ids']) for r in rows),
                'sampled_traces': sum(s['decision'] == 'sample' for s in snapshots.values()),
                'kept_spans': sum(s['kept_span_count'] for s in snapshots.values()),
                'changed_decision_trace_ids': [r['trace_id'] for r in rows if r['decision_differs_from_full_trace']]},
            'traces': rows}


def arrival_assumptions():
    """Keep the bounded timing and reference-inventory limits with saved reports."""
    return {
        'corpus_completeness': 'Missing means absent from the schedule relative to the supplied reference corpus only; real completeness is not verified.',
        'evaluation': 'One policy evaluation per trace at first arrival plus wait, using only spans received strictly before that deadline.',
        'online_timing_or_cache_equivalence': False,
        'production_savings_estimated': False,
        'collector_equivalence': 'Synthetic timing subset only; no Collector tick scheduling, caches, early matches, eviction, or root acceleration.',
        'byte_scope': 'No serialized byte or cost estimate is made for arrival replay.'}


def compare_arrivals(corpus, policies, schedule, baseline_wait_ms, candidate_wait_ms):
    """Compare two waits over one validated inventory and arrival schedule.

    Finalized decision flips require two snapshots. Unresolved states are
    reported separately and never treated as drop votes or EOF decisions.
    """
    baseline_wait = _wait_ns(baseline_wait_ms, 'baseline_wait_ms')
    candidate_wait = _wait_ns(candidate_wait_ms, 'candidate_wait_ms')
    expected, arrivals, cutoff = _validate_arrivals(corpus, schedule)
    before = _replay_arrivals(corpus, policies, expected, arrivals, cutoff, baseline_wait)
    after = _replay_arrivals(corpus, policies, expected, arrivals, cutoff, candidate_wait)
    changed, state_changes, unresolved = [], [], []
    both_decided = newly_sampled = newly_dropped = 0
    for a, b in zip(before['traces'], after['traces']):
        baseline = a['decision_snapshot']
        candidate = b['decision_snapshot']
        evidence = {'trace_id': a['trace_id'], 'baseline': a, 'candidate': b}
        if a['state'] != b['state']:
            state_changes.append(evidence)
        if baseline is not None and candidate is not None:
            both_decided += 1
            if baseline['decision'] != candidate['decision']:
                changed.append(evidence)
                newly_sampled += candidate['decision'] == 'sample'
                newly_dropped += candidate['decision'] == 'drop'
        else:
            unresolved.append({'trace_id': a['trace_id'],
                               'baseline_state': a['state'], 'candidate_state': b['state'],
                               'baseline_decision': baseline['decision'] if baseline is not None else None,
                               'candidate_decision': candidate['decision'] if candidate is not None else None})
    assumptions = arrival_assumptions()
    assumptions['comparison'] = ('Decision flips require both waits to finalize; unresolved states are separate. '
                                 'Kept span delta includes state resolution and differing on-time subsets.')
    return {'schema_version': 1, 'mode': 'arrival_order_offline_comparison',
            'fingerprints': {
                **{key: before['fingerprints'][key] for key in [
                    'version', 'algorithm', 'corpus_source_sha256',
                    'effective_config_sha256', 'arrival_schedule_sha256']},
                'baseline_effective_arrival_model_sha256': before['fingerprints']['effective_arrival_model_sha256'],
                'candidate_effective_arrival_model_sha256': after['fingerprints']['effective_arrival_model_sha256']},
            'assumptions': assumptions, 'baseline': before, 'candidate': after,
            'comparison': {
                'both_decided_trace_count': both_decided,
                'changed_decision_count': len(changed), 'changed_decisions': changed,
                'newly_sampled_traces': newly_sampled, 'newly_dropped_traces': newly_dropped,
                'state_change_count': len(state_changes), 'state_changes': state_changes,
                'unresolved_trace_count': len(unresolved), 'unresolved_traces': unresolved,
                'kept_span_delta': after['summary']['kept_spans'] - before['summary']['kept_spans']}}
