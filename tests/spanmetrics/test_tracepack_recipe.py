# SPDX-License-Identifier: Apache-2.0
"""End-to-end recipe: `otelwrench tracepack` bundles a Jaeger capture, `otelwrench spanmetrics` turns it into metrics."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import otelwrench

RECIPE = Path(__file__).resolve().parents[2] / 'examples/spanmetrics/tracepack-review/replay.py'


def _tracepack_available():
    try:
        return importlib.util.find_spec('otelwrench.tracepack.cli') is not None
    except ImportError:
        return False


@unittest.skipUnless(_tracepack_available(), 'otelwrench tracepack (Python port) is not part of this build')
class TracepackRecipeTests(unittest.TestCase):
    def setUp(self):
        evidence = os.environ.get('RECIPE_EVIDENCE_DIR')
        if evidence:
            self.directory = Path(tempfile.mkdtemp(prefix='recipe-', dir=evidence))
        else:
            self.temp = tempfile.TemporaryDirectory()
            self.addCleanup(self.temp.cleanup)
            self.directory = Path(self.temp.name)

    def run_recipe(self, name, *extra):
        args = [sys.executable, str(RECIPE), '--output-dir', str(self.directory / name), '--side', 'candidate',
                '--python', sys.executable]
        argv = [*args, *extra]
        result = subprocess.run(argv, cwd=self.directory, capture_output=True, text=True, timeout=600)
        log = self.directory / f'{name}-{len(list(self.directory.glob("*.invocation.json")))}'
        log.with_suffix('.stdout.log').write_text(result.stdout)
        log.with_suffix('.stderr.log').write_text(result.stderr)
        log.with_suffix('.invocation.json').write_text(json.dumps({
            'argv': argv, 'cwd': str(self.directory), 'exitCode': result.returncode,
            'stdout': str(log.with_suffix('.stdout.log')), 'stderr': str(log.with_suffix('.stderr.log')),
            'review': str(self.directory / name / 'review.json')}, indent=2) + '\n')
        return result, self.directory / name

    def test_installed_recipe_both_sides_and_rehashed_tampering(self):
        for side in ('baseline', 'candidate'):
            proc, output = self.run_recipe(side, '--side', side)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            review = json.loads((output / 'review.json').read_text())
            self.assertEqual(review['status'], 'verified')
            self.assertEqual(review['selected_side'], side)
            self.assertEqual(review['business_oracle'], 'passed')
            self.assertEqual(review['loss_report']['losses'],
                             [{'path': '$.total', 'reason': 'Jaeger query metadata dropped'}])
            self.assertEqual((output / 'selected.prom').read_bytes(), (output / 'native.prom').read_bytes())
            commands = review['commands']
            self.assertTrue(all(c['exitCode'] == 0 for c in commands))
            self.assertTrue(all(Path(c['stdout']).is_file() and Path(c['stderr']).is_file() for c in commands))
            self.assertEqual(review['artifacts']['tracepack']['version'], otelwrench.__version__)
            self.assertEqual(review['artifacts']['spanmetrics']['version'], otelwrench.__version__)
        bundle = json.loads((output / 'bundle.json').read_text())
        for member in ('normalized', 'lossReport'):
            changed = copy.deepcopy(bundle)
            if member == 'normalized':
                changed[member]['resourceSpans'][0]['scopeSpans'][0]['spans'][0]['name'] = 'tampered'
            else:
                changed[member]['losses'] = []
            # Fixture has ASCII keys/integer data: this is exactly tracepack's
            # sorted/no-whitespace member encoding, not a second converter.
            data = json.dumps(changed[member], sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
            changed['sha256'][member] = hashlib.sha256(data).hexdigest()
            path = self.directory / (member + '.json')
            path.write_text(json.dumps(changed))
            proc, rejected = self.run_recipe(member, '--bundle', str(path))
            self.assertEqual(proc.returncode, 1, proc.stderr)
            review = json.loads((rejected / 'review.json').read_text())
            self.assertEqual(review['status'], 'rejected')
            self.assertFalse((rejected / 'selected.prom').exists())
            self.assertFalse((rejected / 'normalized.json').exists())
            self.assertEqual(review['commands'][-1]['exitCode'], 1)
            self.assertIn('does not match offline conversion replay',
                          Path(review['commands'][-1]['stderr']).read_text())
            self.assertFalse(any('compare' in c['argv'] for c in review['commands']))

    def test_existing_output_refused(self):
        proc, output = self.run_recipe('first')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        review = json.loads((output / 'review.json').read_text())
        sentinel = output / 'sentinel'
        sentinel.write_text('preserve')
        proc, _ = self.run_recipe('first')
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(sentinel.read_text(), 'preserve')
        self.assertEqual(json.loads((output / 'review.json').read_text()), review)


if __name__ == '__main__':
    unittest.main()
