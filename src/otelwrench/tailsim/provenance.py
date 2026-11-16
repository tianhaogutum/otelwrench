"""Check saved input identities, without authenticating report evidence."""

from pathlib import Path
import re

from .arrivals import compare_arrivals, replay_arrivals
from .fingerprints import replay_fingerprints
from .validation import InputError, array, integer, obj, parse_json, string

REPLAY_DIGESTS = ('corpus_source_sha256', 'effective_config_sha256')
REPLAY_COMPARISON_DIGESTS = (REPLAY_DIGESTS[0], 'baseline_effective_config_sha256',
                             'candidate_effective_config_sha256')
ARRIVAL_DIGESTS = (*REPLAY_DIGESTS,
                   'arrival_schedule_sha256', 'effective_arrival_model_sha256')
COMPARISON_DIGESTS = (*ARRIVAL_DIGESTS[:3], 'baseline_effective_arrival_model_sha256',
                      'candidate_effective_arrival_model_sha256')


def _fingerprints(value, path, digests):
    fields = {'version', 'algorithm', *digests}
    obj(value, path, fields, fields)
    integer(value['version'], path + '.version', minimum=1, maximum=1)
    if value['algorithm'] != 'sha256':
        raise InputError(f'{path}.algorithm: expected sha256')
    for key in digests:
        digest = value[key]
        if not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None:
            raise InputError(f'{path}.{key}: expected 64 lowercase hexadecimal characters')
    return value


def load_replay_fingerprints(path):
    """Validate a static replay envelope; its evidence contents stay opaque."""
    report = parse_json(Path(path).read_bytes().decode('utf-8'))
    return _replay_fingerprints(report, 'saved replay report')


def _replay_fingerprints(report, path):
    fields = {'schema_version', 'mode', 'fingerprints', 'assumptions',
              'byte_measure', 'summary', 'traces'}
    obj(report, path, fields, fields)
    integer(report['schema_version'], path + '.schema_version', minimum=1, maximum=1)
    if report['mode'] != 'complete_trace_offline':
        raise InputError(f'{path}.mode: expected complete_trace_offline')
    for key in ('assumptions', 'summary'):
        if not isinstance(report[key], dict):
            raise InputError(f'{path}.{key}: expected an object')
    string(report['byte_measure'], path + '.byte_measure')
    array(report['traces'], path + '.traces')
    return _fingerprints(report['fingerprints'], path + '.fingerprints', REPLAY_DIGESTS)


def load_comparison_fingerprints(path):
    """Validate a static comparison and both embedded replay identity blocks."""
    return _comparison_fingerprints(parse_json(Path(path).read_bytes().decode('utf-8')))


def _comparison_fingerprints(report):
    fields = {'schema_version', 'mode', 'fingerprints', 'assumptions',
              'byte_measure', 'baseline', 'candidate', 'comparison'}
    obj(report, 'saved comparison report', fields, fields)
    integer(report['schema_version'], 'report.schema_version', minimum=1, maximum=1)
    if report['mode'] != 'complete_trace_offline_comparison':
        raise InputError('report.mode: expected complete_trace_offline_comparison')
    for key in ('assumptions', 'comparison'):
        if not isinstance(report[key], dict):
            raise InputError(f'report.{key}: expected an object')
    string(report['byte_measure'], 'report.byte_measure')
    return {'fingerprints': _fingerprints(report['fingerprints'], 'report.fingerprints',
                                          REPLAY_COMPARISON_DIGESTS),
            **{side: _replay_fingerprints(report[side], 'report.' + side)
               for side in ('baseline', 'candidate')}}


def load_arrival_fingerprints(path):
    """Validate the saved replay envelope and exact version-1 hash contract.

    Evidence containers are opaque; provenance cannot authenticate their
    contents. Do not turn a valid hash block into a claim about decisions.
    """
    return _arrival_fingerprints(parse_json(Path(path).read_bytes().decode('utf-8')),
                                 'saved arrival report')


def _arrival_fingerprints(report, path):
    fields = {'schema_version', 'mode', 'fingerprints', 'model', 'assumptions', 'summary', 'traces'}
    obj(report, path, fields, fields)
    integer(report['schema_version'], path + '.schema_version', minimum=1, maximum=1)
    if report['mode'] != 'arrival_order_offline':
        raise InputError(f'{path}.mode: expected arrival_order_offline')
    for key in ('model', 'assumptions', 'summary'):
        if not isinstance(report[key], dict):
            raise InputError(f'{path}.{key}: expected an object')
    array(report['traces'], path + '.traces')
    return _fingerprints(report['fingerprints'], path + '.fingerprints', ARRIVAL_DIGESTS)


def load_arrival_comparison_fingerprints(path):
    """Validate the wrapper and both embedded envelopes/hash blocks fully."""
    return _arrival_comparison_fingerprints(parse_json(Path(path).read_bytes().decode('utf-8')))


def _arrival_comparison_fingerprints(report):
    fields = {'schema_version', 'mode', 'fingerprints', 'assumptions',
              'baseline', 'candidate', 'comparison'}
    obj(report, 'saved arrival comparison', fields, fields)
    integer(report['schema_version'], 'report.schema_version', minimum=1, maximum=1)
    if report['mode'] != 'arrival_order_offline_comparison':
        raise InputError('report.mode: expected arrival_order_offline_comparison')
    for key in ('assumptions', 'comparison'):
        if not isinstance(report[key], dict):
            raise InputError(f'report.{key}: expected an object')
    return {'fingerprints': _fingerprints(report['fingerprints'], 'report.fingerprints', COMPARISON_DIGESTS),
            **{side: _arrival_fingerprints(report[side], 'report.' + side)
               for side in ('baseline', 'candidate')}}


def _checks(reported, supplied, digests):
    return {key: {'reported_sha256': reported[key], 'supplied_sha256': supplied[key],
                  'matches': reported[key] == supplied[key]} for key in digests}


def verify_replay(reported, corpus, policies):
    """Recompute static identities from validated inputs, without evaluating evidence."""
    checks = _checks(reported, replay_fingerprints(corpus, policies), REPLAY_DIGESTS)
    return {'schema_version': 1, 'mode': 'replay_provenance_verification',
            'fingerprint_version': 1, 'algorithm': 'sha256',
            'matches': all(check['matches'] for check in checks.values()), 'checks': checks}


def verify_comparison(reported, corpus, baseline, candidate):
    """Independently check seven identities; evidence is opaque and unauthenticated."""
    before, after = replay_fingerprints(corpus, baseline), replay_fingerprints(corpus, candidate)
    wrapper = {'corpus_source_sha256': corpus.source_sha256,
               'baseline_effective_config_sha256': before['effective_config_sha256'],
               'candidate_effective_config_sha256': after['effective_config_sha256']}
    checks = {'fingerprints': _checks(reported['fingerprints'], wrapper, REPLAY_COMPARISON_DIGESTS),
              'baseline': _checks(reported['baseline'], before, REPLAY_DIGESTS),
              'candidate': _checks(reported['candidate'], after, REPLAY_DIGESTS)}
    return {'schema_version': 1, 'mode': 'comparison_provenance_verification',
            'fingerprint_version': 1, 'algorithm': 'sha256',
            'matches': all(check['matches'] for section in checks.values() for check in section.values()),
            'checks': checks}


def verify_arrivals(reported, corpus, policies, schedule, decision_wait_ms):
    """Recompute via the replay validation path, including deadline overflow."""
    supplied = replay_arrivals(corpus, policies, schedule, decision_wait_ms)['fingerprints']
    checks = _checks(reported, supplied, ARRIVAL_DIGESTS)
    return {'schema_version': 1, 'mode': 'arrival_provenance_verification',
            'fingerprint_version': 1, 'algorithm': 'sha256',
            'matches': all(check['matches'] for check in checks.values()), 'checks': checks}


def verify_arrival_comparison(reported, corpus, policies, schedule, baseline_wait_ms, candidate_wait_ms):
    """Check all declared identities against validated inputs, not saved evidence."""
    supplied = compare_arrivals(corpus, policies, schedule, baseline_wait_ms, candidate_wait_ms)
    checks = {'fingerprints': _checks(reported['fingerprints'], supplied['fingerprints'], COMPARISON_DIGESTS),
              **{side: _checks(reported[side], supplied[side]['fingerprints'], ARRIVAL_DIGESTS)
                 for side in ('baseline', 'candidate')}}
    return {'schema_version': 1, 'mode': 'arrival_comparison_provenance_verification',
            'fingerprint_version': 1, 'algorithm': 'sha256',
            'matches': all(check['matches'] for section in checks.values() for check in section.values()),
            'checks': checks}
