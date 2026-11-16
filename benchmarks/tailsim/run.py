"""Reproducible finite-fixture retention experiment, with a separate oracle.

Run from the repository root: python benchmarks/tailsim/run.py
Only synthetic local files are written. No external services or model downloads.
The oracle uses standard-library JSON and arithmetic; it imports no tailsim code.
"""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def synthetic():
    spans = []
    epoch = 1_700_000_000_000_000_000
    for i in range(1, 101):
        start = epoch + i * 1_000_000_000
        # Root itself always lasts 10 ms. A child sets whole-trace duration/error.
        slow = i % 4 == 0
        for child in (False, True):
            end = start + (600_000_000 if slow and child else 10_000_000)
            spans.append({'traceId': f'{i:032x}', 'spanId': f'{i * 2 + child:016x}',
                          'name': 'child' if child else 'root', 'kind': 3 if child else 2,
                          'startTimeUnixNano': str(start), 'endTimeUnixNano': str(end),
                          'status': {'code': 2 if child and i % 10 == 0 else 1},
                          'attributes': [{'key': 'url.path', 'value': {'stringValue': '/health' if i % 25 == 0 else '/checkout'}},
                                         {'key': 'payload', 'value': {'stringValue': 'synthetic-λ-' + 'x' * (i % 7)}}]})
    return {'resourceSpans': [{'resource': {'attributes': [{'key': 'service.name', 'value': {'stringValue': 'synthetic-shop'}}]},
                               'scopeSpans': [{'scope': {'name': 'tailsim-experiment', 'version': '1'}, 'spans': spans}]}]}


def oracle(document, ids):
    filtered = copy.deepcopy(document)
    all_spans = filtered['resourceSpans'][0]['scopeSpans'][0]['spans']
    filtered['resourceSpans'][0]['scopeSpans'][0]['spans'] = [s for s in all_spans if int(s['traceId'], 16) in ids]
    return len(encoded(filtered))


def run_compare(candidate):
    result = subprocess.run([sys.executable, '-m', 'otelwrench', 'tailsim', 'compare', str(HERE / 'traces.json'),
                             '--baseline', str(ROOT / 'examples/tailsim/baseline.yaml'),
                             '--candidate', str(ROOT / candidate)],
                            cwd=ROOT, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def require(condition, message):
    if not condition:
        raise RuntimeError('independent verification failed: ' + message)


def main():
    doc = synthetic()
    fixture = (json.dumps(doc, indent=2, ensure_ascii=False) + '\n').encode()
    (HERE / 'traces.json').write_bytes(fixture)
    expected_all = set(range(1, 101))
    expected_errors = {i for i in expected_all if i % 10 == 0}
    expected_selective = {i for i in expected_all if i % 10 == 0 or i % 4 == 0}
    expected_drop = {i for i in expected_selective if i % 25 != 0}
    results = []
    for config, ids in [('examples/tailsim/policy.yaml', expected_selective), ('examples/tailsim/drop.yaml', expected_drop)]:
        report = run_compare(config)
        base, cand = report['baseline']['summary'], report['candidate']['summary']
        observed_ids = {int(t['trace_id'], 16) for t in report['candidate']['traces'] if t['decision'] == 'sample'}
        require(observed_ids == ids, 'sampled trace ID set')
        require(base['kept_traces'] == 100 and base['kept_spans'] == 200, 'baseline counts')
        require(base['serialized_kept_bytes'] == oracle(doc, expected_all), 'baseline bytes')
        require(cand['kept_traces'] == len(ids) and cand['kept_spans'] == 2 * len(ids), 'candidate counts')
        require(cand['error_retention']['kept'] == len(ids & expected_errors), 'error retention')
        require(cand['error_retention']['total'] == 10, 'error denominator')
        require(cand['serialized_kept_bytes'] == oracle(doc, ids), 'candidate bytes')
        require(report['comparison']['changed_trace_count'] == 100 - len(ids), 'changed traces')
        require(report['comparison']['error_traces_lost'] == [f'{i:032x}' for i in sorted(expected_errors - ids)], 'lost error IDs')
        require(report['comparison']['serialized_kept_byte_delta'] == oracle(doc, ids) - oracle(doc, expected_all), 'byte delta')
        for row in report['candidate']['traces']:
            i = int(row['trace_id'], 16)
            require(row['duration_ns'] == (600_000_000 if i % 4 == 0 else 10_000_000), 'complete-trace latency')
            require(row['error'] == (i % 10 == 0), 'child error aggregation')
        results.append({'candidate_config': config, 'baseline': base, 'candidate': cand,
                        'comparison': report['comparison'], 'independent_verification': 'passed'})
    output = {'fixture': 'benchmarks/tailsim/traces.json', 'fixture_sha256': hashlib.sha256(fixture).hexdigest(),
              'trace_count': 100, 'span_count': 200, 'experiment': 'finite synthetic retention; no throughput or production forecast',
              'results': results}
    (HERE / 'results.json').write_text(json.dumps(output, indent=2) + '\n')
    print(json.dumps({'verified_candidates': len(results), 'trace_count': 100, 'span_count': 200,
                      'results_file': 'benchmarks/tailsim/results.json'}, indent=2))


if __name__ == '__main__':
    main()
