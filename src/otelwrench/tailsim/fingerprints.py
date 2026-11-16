"""Version-1 identities of validated replay inputs (see README).

Normalize the parsed policies, not YAML text or corpus-dependent evaluations.
Preserve names/order for report evidence; canonicalize only defaults and sets.
"""

import hashlib

from .validation import canonical_bytes


def normalized_policy(policy):
    kind = policy.kind
    if kind == 'always_sample':
        body = {}
    elif kind == 'latency':
        body = {'threshold_ms': policy.params['threshold_ms'],
                'upper_threshold_ms': policy.params.get('upper_threshold_ms', 0)}
    elif kind == 'status_code':
        body = {'status_codes': sorted(set(policy.params['status_codes']))}
    elif kind == 'string_attribute':
        body = {'key': policy.params['key'], 'values': sorted(set(policy.params['values']))}
    elif kind == 'not':
        body = {'not_sub_policy': normalized_policy(policy.children[0])}
    else:
        child_key = {'and': 'and_sub_policy', 'drop': 'drop_sub_policy'}[kind]
        body = {child_key: [normalized_policy(child) for child in policy.children]}
    return {'name': policy.name, 'type': kind, kind: body}


def effective_config_sha256(policies):
    config = {'processors': {'tail_sampling': {
        'policies': [normalized_policy(policy) for policy in policies]}}}
    return hashlib.sha256(canonical_bytes(config)).hexdigest()


def replay_fingerprints(corpus, policies):
    return {'version': 1, 'algorithm': 'sha256',
            'corpus_source_sha256': corpus.source_sha256,
            'effective_config_sha256': effective_config_sha256(policies)}


def arrival_fingerprints(corpus, policies, arrivals, cutoff, decision_wait_ms):
    """Hash the complete validated schedule and integer fixed-wait model.

    Use the resolved lowercase IDs and integer times, including arrivals that
    are late or pending. The source identity is the full reference corpus.
    """
    rows = [{'at_unix_nano': at, 'trace_id': tid, 'span_id': sid}
            for tid, times in arrivals.items() for sid, at in times.items()]
    rows.sort(key=lambda row: (row['at_unix_nano'], row['trace_id'], row['span_id']))
    schedule = {'schema_version': 1, 'observation_end_unix_nano': cutoff, 'arrivals': rows}
    model = {'name': 'first_arrival_fixed_wait_v1', 'decision_wait_ms': decision_wait_ms}
    return {**replay_fingerprints(corpus, policies),
            'arrival_schedule_sha256': hashlib.sha256(canonical_bytes(schedule)).hexdigest(),
            'effective_arrival_model_sha256': hashlib.sha256(canonical_bytes(model)).hexdigest()}
