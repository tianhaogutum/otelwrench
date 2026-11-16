# SPDX-License-Identifier: Apache-2.0
"""Explicit bounded stdin, capture-wide rejection, and publication contracts."""
from copy import deepcopy
import hashlib
import io
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from prometheus_client.parser import text_string_to_metric_families
import test_cli
from .test_cli import CLI, ROOT
from .test_core import document, encode, span
from .test_jsonl import jsonl, split_requests
from otelwrench.spanmetrics import cli


class StdinTests(unittest.TestCase):
    setUp = test_cli.CliTests.setUp

    def run_stdin(self, data, *options, comparison=False, named=False, default_format=True, stdin=None):
        command = [*CLI] + (['compare'] if comparison else [])
        command += ['--input', '-'] if named else ['-']
        command += ['--output', str(self.report if comparison else self.output)]
        if default_format:
            command += ['--input-format', 'jsonl']
        return subprocess.run(command + list(map(str, options)), input=data if stdin is None else None,
                              stdin=stdin, capture_output=True, timeout=60, cwd=self.directory)

    def test_stdin_forms_match_file_metrics_and_hash_complete_capture(self):
        data = (ROOT / 'examples/spanmetrics/traces.jsonl').read_bytes()
        for named in (False, True):
            with self.subTest(named=named):
                proc = self.run_stdin(data, '--force', named=named)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                report = json.loads(proc.stdout)
                self.assertEqual(report, json.loads(self.report.read_bytes()))
                self.assertEqual(report['input'], '-')
                self.assertEqual(report['input_sha256'], hashlib.sha256(data).hexdigest())
                self.assertTrue(report['record_counts_complete'])
                self.assertTrue(report['metrics_emitted'])
                self.assertEqual([report[k] for k in ('unique_records', 'errors', 'duration_sum_seconds', 'groups', 'series')],
                                 [2, 1, '0.4', 2, 16])
                file_proc = subprocess.run([*CLI, str(ROOT / 'examples/spanmetrics/traces.jsonl'), '--input-format', 'jsonl',
                                            '--output', str(self.directory / 'file.prom'), '--force'],
                                           capture_output=True, timeout=60)
                self.assertEqual(file_proc.returncode, 0, file_proc.stderr)
                self.assertEqual(self.output.read_bytes(), (self.directory / 'file.prom').read_bytes())
                samples = [s for f in text_string_to_metric_families(self.output.read_text()) for s in f.samples]
                self.assertEqual(len(samples), 16)
                self.assertEqual(sum(s.value for s in samples if s.name == 'spanmetrics_calls_total'), 2)
                self.assertEqual(sum(s.value for s in samples if s.name == 'spanmetrics_errors_total'), 1)
                self.assertEqual(report['prometheus_sha256'], hashlib.sha256(self.output.read_bytes()).hexdigest())

    def test_rejected_stdin_preserves_metrics_and_hashes_only_complete_reads(self):
        first = document([span()])
        conflict = deepcopy(first)
        conflict['resourceSpans'][0]['scopeSpans'][0]['spans'][0]['name'] = 'changed'
        normal = jsonl(first, first, document([span(2, name='other')]))
        bad_span = document([dict(span(2), name='')])
        cases = [
            (normal, ('--max-input-bytes', str(len(normal) - 1)), False, 'byte cap', False),
            (normal, ('--max-spans', '2'), True, 'span cap', False),
            (jsonl(first, bad_span, first), ('--max-spans', '2'), True, 'span cap', False),
            (normal, ('--max-series', '8'), True, 'series cap', True),
            (jsonl(first, conflict), (), True, 'conflicting duplicate', True),
            (jsonl(first) + b'{\n' + jsonl(first), (), True, 'line 2', False),
            (jsonl(first) + b'\xff\n', (), True, 'line 2', False),
            (jsonl(first) + b'\n', (), True, 'line 2', False),
            (b'', (), True, 'empty JSONL', False),
            (b'{"resourceSpans":[]}\n' + encode(first) + b'\n',
             ('--max-line-bytes', str(len(encode(first)) - 1)), True, 'line byte cap', False),
        ]
        for data, options, hashed, diagnostic, complete in cases:
            for existing in (False, True):
                with self.subTest(options=options, diagnostic=diagnostic, existing=existing):
                    self.output.unlink(missing_ok=True)
                    self.report.unlink(missing_ok=True)
                    if existing:
                        self.output.write_bytes(b'previous metrics\n')
                    proc = self.run_stdin(data, '--force', *options)
                    self.assertEqual(proc.returncode, 1, proc.stderr)
                    report = json.loads(proc.stdout)
                    self.assertEqual(report, json.loads(self.report.read_bytes()))
                    self.assertEqual(report['status'], 'rejected')
                    self.assertEqual(report['record_counts_complete'], complete)
                    self.assertEqual('input_sha256' in report, hashed)
                    if hashed:
                        self.assertEqual(report['input_sha256'], hashlib.sha256(data).hexdigest())
                    self.assertTrue(any(diagnostic in d for d in report['diagnostics']), report['diagnostics'])
                    self.assertFalse(report['metrics_emitted'])
                    self.assertNotIn('prometheus_sha256', report)
                    self.assertEqual(self.output.exists(), existing)
                    if existing:
                        self.assertEqual(self.output.read_bytes(), b'previous metrics\n')
                    self.assertFalse(list(self.directory.glob('.spanmetrics-*')))

    def test_exact_byte_line_and_global_span_series_boundaries(self):
        line = encode(document([span(name='☃')])).replace(b'\\u2603', '☃'.encode())
        for ending in (b'', b'\n', b'\r\n'):
            data = line + ending
            proc = self.run_stdin(data, '--max-input-bytes', str(len(data)),
                                  '--max-line-bytes', str(len(line)), '--max-spans', '1',
                                  '--max-series', '8', '--dry-run')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)['input_sha256'], hashlib.sha256(data).hexdigest())
        first = document([span()])
        data = jsonl(first, first, document([span(2, name='other')]))
        proc = self.run_stdin(data, '--max-input-bytes', str(len(data)), '--max-spans', '3',
                              '--max-series', '16', '--dry-run')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual([report[k] for k in ('input_records', 'unique_records', 'duplicate_records', 'series')],
                         [3, 2, 1, 16])
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_comparison_stdin_reuses_pair_and_suppresses_rejected_counts(self):
        data = jsonl(*split_requests(json.loads((ROOT / 'examples/spanmetrics/dimensions.json').read_bytes())))
        options = ('--dimensions-file', ROOT / 'examples/spanmetrics/dimension-pair.json', '--force')
        for named in (False, True):
            proc = self.run_stdin(data, *options, comparison=True, named=named)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report, json.loads(self.report.read_bytes()))
            self.assertEqual(report['series_delta'], 24)
            self.assertEqual([report['configurations'][side]['series'] for side in ('baseline', 'candidate')], [16, 40])
            self.assertEqual(report['input_sha256'], hashlib.sha256(data).hexdigest())
        for extra, hashed in [(('--max-input-bytes', '10'), False), (('--max-spans', '1'), True),
                              (('--max-series', '16'), True), (('--max-line-bytes', '10'), True)]:
            proc = self.run_stdin(data, *options, *extra, comparison=True)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertFalse(report['comparison_complete'])
            self.assertIsNone(report['series_delta'])
            self.assertEqual(report['partitions'], [])
            self.assertEqual('input_sha256' in report, hashed)
            for side in ('baseline', 'candidate'):
                self.assertIsNone(report['configurations'][side]['groups'])
                self.assertIsNone(report['configurations'][side]['series'])
        self.assertFalse(self.output.exists())

    def test_options_require_one_explicit_source_and_jsonl_for_stdin(self):
        for comparison in (False, True):
            for options in ((), ('--input-format', 'json')):
                proc = self.run_stdin(b'', *options, comparison=comparison, default_format=False)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn(b'jsonl', proc.stderr)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.report.exists())
            base = [*CLI] + (['compare'] if comparison else [])
            for inputs in ([], [str(ROOT / 'examples/spanmetrics/traces.jsonl'), '--input', '-']):
                proc = subprocess.run(base + inputs + ['--input-format', 'jsonl', '--output', str(self.output)],
                                      input=b'', capture_output=True, timeout=60)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, b'')
        # Named files also work; stdin is never inferred when a source is omitted.
        proc = subprocess.run([*CLI, '--input', str(ROOT / 'examples/spanmetrics/traces.json'),
                               '--output', str(self.output), '--dry-run'], capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_literal_dash_file_is_distinct_from_stdin_marker(self):
        (self.directory / '-').write_bytes(jsonl(document([span()])))
        for comparison in (False, True):
            for source in (['./-'], ['--input', './-']):
                command = [*CLI] + (['compare'] if comparison else [])
                proc = subprocess.run(command + source + ['--input-format', 'jsonl', '--output',
                                      str(self.report if comparison else self.output), '--dry-run'],
                                      input=b'invalid stdin', capture_output=True, timeout=60, cwd=self.directory)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(json.loads(proc.stdout)['unique_records'], 1)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_dry_run_and_preflight_paths_preserve_files(self):
        data = (ROOT / 'examples/spanmetrics/traces.jsonl').read_bytes()
        self.output.write_text('metrics sentinel')
        self.report.write_text('report sentinel')
        proc = self.run_stdin(data, '--dry-run')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(json.loads(proc.stdout)['metrics_emitted'])
        self.assertEqual(self.output.read_text(), 'metrics sentinel')
        self.assertEqual(self.report.read_text(), 'report sentinel')
        for options in ((), ('--force', '--report', self.output),
                        ('--force', '--report', self.directory / 'missing' / 'report.json')):
            proc = self.run_stdin(data, *options)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(proc.stdout, b'')
            self.assertEqual(self.output.read_text(), 'metrics sentinel')
            self.assertEqual(self.report.read_text(), 'report sentinel')

    def test_redirected_regular_stdin_cannot_be_overwritten_via_aliases(self):
        source = self.directory / 'capture.jsonl'
        source.write_bytes((ROOT / 'examples/spanmetrics/traces.jsonl').read_bytes())
        for comparison in (False, True):
            for target in (self.output, self.report) if not comparison else (self.report,):
                os.link(source, target)
                with source.open('rb') as stream:
                    proc = self.run_stdin(None, '--force', comparison=comparison, stdin=stream)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn(b'overwrite input', proc.stderr)
                self.assertEqual(source.read_bytes(), (ROOT / 'examples/spanmetrics/traces.jsonl').read_bytes())
                target.unlink()
                self.assertFalse(self.output.exists())
                self.assertFalse(self.report.exists())

    def test_short_reads_wait_for_eof_and_io_failure_never_publishes(self):
        data = jsonl(document([]))

        class ShortReads(io.BytesIO):
            def __init__(self, payload, fail=False):
                super().__init__(payload)
                self.sizes = []
                self.fail = fail

            def read(self, size=-1):
                # No output, even after a complete request, until EOF is observed.
                self_test.assertFalse(self_test.output.exists())
                self_test.assertFalse(self_test.report.exists())
                self.sizes.append(size)
                if self.fail and self.tell() >= 4:
                    raise OSError('synthetic stdin read failure')
                return super().read(min(size, 2))

        self_test = self
        for comparison in (False, True):
            for fail in (False, True):
                self.output.unlink(missing_ok=True)
                self.report.unlink(missing_ok=True)
                stream = ShortReads(data, fail)
                args = (['compare'] if comparison else []) + ['-', '--input-format', 'jsonl',
                        '--output', str(self.report if comparison else self.output)]
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(cli.sys, 'stdin', type('Stdin', (), {'buffer': stream})()), \
                     patch.object(cli.sys, 'stdout', stdout), patch.object(cli.sys, 'stderr', stderr):
                    code = cli.main(args)
                self.assertEqual(code, 2 if fail else 0, stderr.getvalue())
                if fail:
                    self.assertEqual(stdout.getvalue(), '')
                    self.assertIn('synthetic stdin read failure', stderr.getvalue())
                else:
                    self.assertEqual(json.loads(stdout.getvalue())['input_sha256'], hashlib.sha256(data).hexdigest())
                self.assertTrue(stream.sizes)
                self.assertTrue(all(0 < size <= 65536 for size in stream.sizes))
                self.assertFalse(stream.closed)
                self.assertEqual(self.output.exists(), not fail and not comparison)
                self.assertEqual(self.report.exists(), not fail)

    def test_oversized_stdin_stops_after_cap_plus_one_without_eof(self):
        class NoEof(io.BytesIO):
            def read(self, size=-1):
                self_test.assertGreater(size, 0)
                self_test.assertLessEqual(self.tell() + size, 11)
                return super().read(size)
        self_test = self
        stream = NoEof(b'x' * 100)
        with patch.object(cli.sys, 'stdin', type('Stdin', (), {'buffer': stream})()), \
             patch.object(cli.sys, 'stdout', io.StringIO()) as stdout, patch.object(cli.sys, 'stderr', io.StringIO()):
            code = cli.main(['-', '--input-format', 'jsonl', '--output', str(self.output), '--max-input-bytes', '10'])
        self.assertEqual(code, 1)
        self.assertEqual(stream.tell(), 11)
        self.assertNotIn('input_sha256', json.loads(stdout.getvalue()))
        self.assertFalse(self.output.exists())
        self.assertTrue(self.report.exists())


if __name__ == '__main__':
    unittest.main()
