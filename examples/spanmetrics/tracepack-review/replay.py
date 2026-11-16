#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Replay the shipped synthetic Jaeger metric review through `otelwrench tracepack` and `otelwrench spanmetrics`.

Both tools run as subprocesses of the Python environment that runs this recipe
(`python -m otelwrench ...`); no converter code is imported here. The independent
Prometheus parser (prometheus-client) checks the published metrics.
"""
import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, doc):
    path.write_text(json.dumps(doc, sort_keys=True, indent=2) + '\n', encoding='utf-8')


def require(condition, message):
    if not condition:
        raise ValueError(message)


class Review:
    def __init__(self, output):
        self.output = output
        self.doc = {'schema_version': 1, 'status': 'rejected', 'commands': [], 'artifacts': {},
                    'scope': 'capture-local counts of all three spans; no production traffic, rates or cost inference',
                    'verification_guarantee': 'reproducibility-only; no authenticity or concurrent-mutation locking'}

    def run(self, label, argv):
        stdout = self.output / 'logs' / (label + '.stdout.log')
        stderr = self.output / 'logs' / (label + '.stderr.log')
        # Every tool process runs in the review directory. Save raw bytes, even
        # on failure; no shell commands, pipes or package/network operations.
        with stdout.open('wb') as out, stderr.open('wb') as err:
            result = subprocess.run(list(map(str, argv)), cwd=self.output, env=dict(os.environ),
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=60)
        self.doc['commands'].append({'label': label, 'argv': list(map(str, argv)), 'cwd': str(self.output),
                                     'exitCode': result.returncode, 'stdout': str(stdout), 'stderr': str(stderr)})
        require(result.returncode == 0, f'{label} failed (exit {result.returncode}); see {stderr}')
        return stdout.read_bytes(), stderr.read_bytes()


def check_tools(review, args):
    """Record the otelwrench version that both subcommands run from."""
    python = args.python
    out, _ = review.run('otelwrench-version', [python, '-c',
        'import json, otelwrench; print(json.dumps({"version": otelwrench.__version__}))'])
    installed = json.loads(out)
    for name in ('tracepack', 'spanmetrics'):
        review.doc['artifacts'][name] = {'command': f'otelwrench {name}', 'version': installed['version']}
    python_version, _ = review.run('python-version', [python, '--version'])
    review.doc['runtime'] = {'python': python_version.decode().strip()}
    return [python, '-m', 'otelwrench', 'tracepack'], [python, '-m', 'otelwrench', 'spanmetrics']


def metric_oracle(path, side):
    # This third-party parser validates exposition syntax and sample identities.
    # Decimal values come from raw sample tokens because that parser uses floats.
    from prometheus_client.parser import text_string_to_metric_families
    text = path.read_text(encoding='utf-8')
    families = list(text_string_to_metric_families(text))
    samples = [sample for family in families for sample in family.samples]
    tokens = [Decimal(line.rsplit(' ', 1)[1]) for line in text.splitlines() if line and not line.startswith('#')]
    require(len(samples) == len(tokens), 'unexpected sample/token count')
    expected = {}
    # Hand-counted fixtures; no values are calculated from either CLI's JSON.
    groups = [('web', 'GET /shop', '2', '0', None, 2, 0, '.4', [1, 2, 2, 2]),
              ('db', 'SELECT', '3', '2', None, 1, 1, '.5', [0, 1, 1, 1])]
    if side == 'candidate':
        groups = [('web', 'GET /shop', '2', '0', 's:a', 1, 0, '.1', [1, 1, 1, 1]),
                  ('web', 'GET /shop', '2', '0', 's:b', 1, 0, '.3', [0, 1, 1, 1]),
                  ('db', 'SELECT', '3', '2', 's:a', 1, 1, '.5', [0, 1, 1, 1])]
    for service, op, kind, status, customer, calls, errors, total, buckets in groups:
        labels = dict(service_name=service, span_name=op, span_kind=kind, status_code=status)
        if customer is not None:
            labels['customer'] = customer
        def add(name, value, extra=None):
            expected[(name, tuple(sorted(dict(labels, **(extra or {})).items())))] = Decimal(value)
        add('spanmetrics_calls_total', calls)
        add('spanmetrics_errors_total', errors)
        add('spanmetrics_duration_seconds_count', calls)
        add('spanmetrics_duration_seconds_sum', total)
        for bound, count in zip(('0.1', '0.5', '1', '+Inf'), buckets):
            add('spanmetrics_duration_seconds_bucket', count, {'le': bound})
    actual = {(s.name, tuple(sorted(s.labels.items()))): token for s, token in zip(samples, tokens)}
    require(len(samples) == len(actual) and actual == expected, 'independent metric oracle failed')


def replay(review, args):
    tracepack, spanmetrics = check_tools(review, args)
    from importlib.metadata import version
    review.doc['parser_version'] = version('prometheus-client')
    out = review.output
    review.doc['selected_side'] = args.side
    for name in ('source.jaeger.json', 'native.otlp.json', 'dimension-pair.json'):
        shutil.copyfile(HERE / name, out / name)
    source, pair, native = (out / name for name in ('source.jaeger.json', 'dimension-pair.json', 'native.otlp.json'))
    bundle_path = out / 'bundle.json'
    if args.bundle:
        shutil.copyfile(args.bundle, bundle_path)
    else:
        review.run('bundle', [*tracepack, 'bundle', source, '--from', 'jaeger', '--output', bundle_path])
    # This is the gate: do not parse/use bundle members or invoke metrics until
    # tracepack has replay-verified them and compared the exact source bytes.
    verified, _ = review.run('verify', [*tracepack, 'verify', bundle_path, '--source', source])
    require(json.loads(verified).get('valid') is True, 'missing verification success')
    bundle = json.loads(bundle_path.read_bytes())
    review.doc['bundle_verification'] = json.loads(verified)
    review.doc['loss_report'] = bundle['lossReport']
    review.doc['hashes'] = {'source': digest(source), 'native_otlp': digest(native), 'config': digest(pair),
                            'bundle': digest(bundle_path), 'bundle_members': bundle['sha256']}
    restored, normalized = out / 'restored.jaeger.json', out / 'normalized.json'
    review.run('extract', [*tracepack, 'extract', bundle_path, '--output', restored])
    require(restored.read_bytes() == source.read_bytes(), 'extracted source differs')
    _, losses = review.run('convert', [*tracepack, 'convert', restored, '--from', 'jaeger', '--to', 'otlp', '--output', normalized])
    loss_report = json.loads(losses)
    save(out / 'conversion-loss.json', loss_report)
    require(loss_report == bundle['lossReport'], 'conversion loss report differs from verified bundle')
    require(loss_report['losses'] == [{'path': '$.total', 'reason': 'Jaeger query metadata dropped'}],
            'unexpected synthetic loss report')
    require(json.loads(normalized.read_bytes()) == bundle['normalized'], 'converted OTLP differs from verified normalization')
    common = ['--dimensions-file', pair, '--buckets', '0.1,0.5,1']
    for label, capture in (('compare', normalized), ('native-compare', native)):
        result, _ = review.run(label, [*spanmetrics, 'compare', capture, *common, '--output', out / (label + '.json')])
        comparison = json.loads(result)
        require(comparison['comparison_complete'] is True and comparison['series_delta'] == 8,
                'unexpected comparison')
        for side, groups, series in (('baseline', 2, 16), ('candidate', 3, 24)):
            require(comparison['configurations'][side]['groups'] == groups and
                    comparison['configurations'][side]['series'] == series, 'unexpected side cardinality')
    # Compare attribution/business content, excluding path/raw-capture validation
    # provenance that is intentionally different between equivalent encodings.
    comparisons = [json.loads((out / (name + '.json')).read_bytes()) for name in ('compare', 'native-compare')]
    for key in ('partitions', 'series_delta', 'unique_records', 'configurations'):
        if key == 'configurations':
            for side in ('baseline', 'candidate'):
                for field in ('groups', 'series', 'dimensions'):
                    require(comparisons[0][key][side][field] == comparisons[1][key][side][field],
                            'native comparison differs')
        else:
            require(comparisons[0][key] == comparisons[1][key], 'native comparison differs')
    for label, capture in (('selected', normalized), ('native', native)):
        report, _ = review.run(label, [*spanmetrics, capture, *common, '--dimensions-side', args.side,
                                     '--dimension-provenance', '--output', out / (label + '.prom')])
        manifest = json.loads(report)
        require(manifest['status'] == 'ok' and manifest['metrics_emitted'] is True, 'metric publication failed')
        require(manifest['dimension_pair_provenance']['sha256'] == digest(pair), 'pair provenance differs')
        require(manifest['prometheus_sha256'] == digest(out / (label + '.prom')), 'metric hash differs')
        metric_oracle(out / (label + '.prom'), args.side)
    require((out / 'selected.prom').read_bytes() == (out / 'native.prom').read_bytes(), 'native business metrics differ')
    review.doc['hashes'].update(normalized=digest(normalized), prometheus=digest(out / 'selected.prom'),
                                conversion_loss=digest(out / 'conversion-loss.json'))
    review.doc['business_oracle'] = 'passed'
    review.doc['status'] = 'verified'


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--python', type=Path, default=Path(sys.executable),
                        help='interpreter of the environment where otelwrench is installed (default: this one)')
    parser.add_argument('--side', choices=('baseline', 'candidate'), required=True)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--bundle', type=Path, help='verify a saved bundle against the shipped source instead of creating one')
    args = parser.parse_args()
    for key, value in vars(args).items():
        if isinstance(value, Path):
            # Keep venv launcher paths: resolving their symlink can select the
            # system interpreter instead of that installed environment.
            setattr(args, key, value.absolute())
    try:
        checkout = HERE.parents[2]
        require(not args.output_dir.resolve().is_relative_to(checkout), 'output directory must be outside the repository')
        args.output_dir.mkdir()  # exclusive new directory; never overwrite an old review
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    review = Review(args.output_dir)
    (args.output_dir / 'logs').mkdir()
    try:
        replay(review, args)
    except (ValueError, OSError, KeyError, ImportError, subprocess.SubprocessError) as exc:
        review.doc['error'] = str(exc)
        print(str(exc), file=sys.stderr)
    finally:
        save(args.output_dir / 'review.json', review.doc)
    return 0 if review.doc['status'] == 'verified' else 1


if __name__ == '__main__':
    sys.exit(main())
