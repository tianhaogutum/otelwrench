"""Compare saved evidence with local regeneration, without authorship claims."""

import math
from itertools import islice
from pathlib import Path

from .arrivals import compare_arrivals, replay_arrivals
from .provenance import (_arrival_comparison_fingerprints, _arrival_fingerprints,
                         _comparison_fingerprints, _replay_fingerprints)
from .replay import compare, replay
from .validation import InputError, parse_json


def _finite_json(value):
    # parse_json rejects NaN/Infinity tokens; JSON exponents can also overflow
    # Python's float parser, even deep inside otherwise opaque evidence.
    if isinstance(value, float) and not math.isfinite(value):
        raise InputError('saved report: nonfinite JSON number is unsupported')
    if isinstance(value, dict):
        for child in value.values():
            _finite_json(child)
    elif isinstance(value, list):
        for child in value:
            _finite_json(child)


def load_replay_evidence(path):
    """Validate the shared envelope, not the producer schema of nested evidence."""
    report = parse_json(Path(path).read_bytes().decode('utf-8'))
    _replay_fingerprints(report, 'saved replay report')
    _finite_json(report)
    return report


def load_comparison_evidence(path):
    """Validate wrapper and both embedded envelopes, keeping finite evidence as data."""
    report = parse_json(Path(path).read_bytes().decode('utf-8'))
    _comparison_fingerprints(report)
    _finite_json(report)
    return report


def load_arrival_evidence(path):
    """Validate the single-wait envelope, keeping finite nested evidence as data."""
    report = parse_json(Path(path).read_bytes().decode('utf-8'))
    _arrival_fingerprints(report, 'saved arrival report')
    _finite_json(report)
    return report


def load_arrival_comparison_evidence(path):
    """Validate wrapper and both arrival envelopes, keeping finite evidence as data."""
    report = parse_json(Path(path).read_bytes().decode('utf-8'))
    _arrival_comparison_fingerprints(report)
    _finite_json(report)
    return report


def _json_equal(saved, regenerated):
    """Ignore mapping order; retain array order and scalar types (bool != int)."""
    if type(saved) is not type(regenerated):
        return False
    if isinstance(saved, dict):
        return saved.keys() == regenerated.keys() and all(
            _json_equal(saved[key], regenerated[key]) for key in saved)
    if isinstance(saved, list):
        return len(saved) == len(regenerated) and all(
            _json_equal(a, b) for a, b in zip(saved, regenerated))
    return saved == regenerated


def _mismatch_paths(saved, regenerated, path=''):
    """Yield value-free JSON Pointers in key-sorted, depth-first order."""
    if type(saved) is not type(regenerated):
        yield {'path': path, 'reason': 'type_mismatch'}
    elif isinstance(saved, dict):
        for key in sorted(saved.keys() | regenerated.keys()):
            child_path = path + '/' + key.replace('~', '~0').replace('/', '~1')
            if key not in saved:
                yield {'path': child_path, 'reason': 'missing_key'}
            elif key not in regenerated:
                yield {'path': child_path, 'reason': 'extra_key'}
            else:
                yield from _mismatch_paths(saved[key], regenerated[key], child_path)
    elif isinstance(saved, list):
        for index in range(max(len(saved), len(regenerated))):
            child_path = path + '/' + str(index)
            if index >= len(saved):
                yield {'path': child_path, 'reason': 'missing_item'}
            elif index >= len(regenerated):
                yield {'path': child_path, 'reason': 'extra_item'}
            else:
                yield from _mismatch_paths(saved[index], regenerated[index], child_path)
    elif saved != regenerated:
        yield {'path': path, 'reason': 'value_mismatch'}


def verify_replay_evidence(reported, corpus, policies, diagnostic_limit=None):
    """All fields checked against supplied inputs, never against saved assertions."""
    if diagnostic_limit is not None and (type(diagnostic_limit) is not int or
                                          not 1 <= diagnostic_limit <= 1000):
        raise InputError('diagnostic limit must be an integer from 1 to 1000')
    regenerated = replay(corpus, policies)
    checks = {key: {'matches': _json_equal(reported[key], value)}
              for key, value in regenerated.items()}
    result = {'schema_version': 1, 'mode': 'replay_evidence_verification',
              'matches': all(check['matches'] for check in checks.values()), 'checks': checks}
    if diagnostic_limit is not None:
        mismatches = list(islice(_mismatch_paths(reported, regenerated), diagnostic_limit + 1))
        result['diagnostics'] = {'limit': diagnostic_limit, 'mismatches': mismatches[:diagnostic_limit],
                                 'truncated': len(mismatches) > diagnostic_limit}
    return result


def verify_comparison_evidence(reported, corpus, baseline, candidate):
    """Check every wrapper field and both full replay reports against local compare."""
    regenerated = compare(corpus, baseline, candidate)
    checks = {key: {'matches': _json_equal(reported[key], value)}
              for key, value in regenerated.items()}
    return {'schema_version': 1, 'mode': 'comparison_evidence_verification',
            'matches': all(check['matches'] for check in checks.values()), 'checks': checks}


def verify_arrival_evidence(reported, corpus, policies, schedule, decision_wait_ms,
                            diagnostic_limit=None):
    """Regenerate the full arrival report using only validated supplied inputs."""
    if diagnostic_limit is not None and (type(diagnostic_limit) is not int or
                                          not 1 <= diagnostic_limit <= 1000):
        raise InputError('diagnostic limit must be an integer from 1 to 1000')
    regenerated = replay_arrivals(corpus, policies, schedule, decision_wait_ms)
    checks = {key: {'matches': _json_equal(reported[key], value)}
              for key, value in regenerated.items()}
    result = {'schema_version': 1, 'mode': 'arrival_evidence_verification',
              'matches': all(check['matches'] for check in checks.values()), 'checks': checks}
    if diagnostic_limit is not None:
        mismatches = list(islice(_mismatch_paths(reported, regenerated), diagnostic_limit + 1))
        result['diagnostics'] = {'limit': diagnostic_limit, 'mismatches': mismatches[:diagnostic_limit],
                                 'truncated': len(mismatches) > diagnostic_limit}
    return result


def verify_arrival_comparison_evidence(reported, corpus, policies, schedule,
                                       baseline_wait_ms, candidate_wait_ms, diagnostic_limit=None):
    """Check all wrapper fields, including both full replays and comparison evidence."""
    if diagnostic_limit is not None and (type(diagnostic_limit) is not int or
                                          not 1 <= diagnostic_limit <= 1000):
        raise InputError('diagnostic limit must be an integer from 1 to 1000')
    regenerated = compare_arrivals(corpus, policies, schedule, baseline_wait_ms, candidate_wait_ms)
    checks = {key: {'matches': _json_equal(reported[key], value)}
              for key, value in regenerated.items()}
    result = {'schema_version': 1, 'mode': 'arrival_comparison_evidence_verification',
              'matches': all(check['matches'] for check in checks.values()), 'checks': checks}
    if diagnostic_limit is not None:
        # One lookahead distinguishes exact fit from omitted differences, without
        # collecting or counting the remainder of untrusted evidence.
        mismatches = list(islice(_mismatch_paths(reported, regenerated), diagnostic_limit + 1))
        result['diagnostics'] = {'limit': diagnostic_limit, 'mismatches': mismatches[:diagnostic_limit],
                                 'truncated': len(mismatches) > diagnostic_limit}
    return result
