# SPDX-License-Identifier: Apache-2.0
"""Synthetic multi-request captures with global validation and publication checks."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import unittest

from .test_core import ROOT, document, encode, span, samples
import test_cli
from otelwrench.spanmetrics.core import Config, InputError, convert
from otelwrench.spanmetrics.comparison import compare


def jsonl(*requests):
    return b'\n'.join(encode(request) for request in requests) + b'\n'


def split_requests(doc):
    requests = []
    for resource in doc['resourceSpans']:
        for scope in resource.get('scopeSpans', []):
            for record in scope.get('spans', []):
                rs, ss = deepcopy(resource), deepcopy(scope)
                ss['spans'] = [record]
                rs['scopeSpans'] = [ss]
                requests.append({'resourceSpans': [rs]})
    return requests


class JsonlTests(unittest.TestCase):
    def test_split_fixture_matches_single_request_report_and_every_sample(self):
        data = (ROOT / 'examples/spanmetrics/traces.jsonl').read_bytes()
        single = convert((ROOT / 'examples/spanmetrics/traces.json').read_bytes())
        result = convert(data, Config(input_format='jsonl'))
        self.assertEqual(result.prometheus, single.prometheus)
        self.assertEqual(samples(result), samples(single))
        self.assertEqual(result.report['input_sha256'], hashlib.sha256(data).hexdigest())
        self.assertIn('JSONL', result.report['input_format'])
        for key in single.report.keys() - {'input_sha256', 'input_format'}:
            self.assertEqual(result.report[key], single.report[key], key)
        self.assertEqual([result.report[k] for k in ('unique_records', 'errors', 'duration_sum_seconds', 'groups', 'series')],
                         [2, 1, '0.4', 2, 16])

    def test_cross_line_duplicates_match_merged_request(self):
        doc = json.loads((ROOT / 'examples/spanmetrics/duplicates.json').read_text())
        data = jsonl(*split_requests(doc))
        result = convert(data, Config(input_format='jsonl'))
        single = convert(encode(doc))
        self.assertEqual(result.prometheus, single.prometheus)
        self.assertEqual([result.report[k] for k in ('input_records', 'unique_records', 'duplicate_records')], [3, 2, 1])

    def test_cross_line_conflicts_include_full_resource_scope_and_schema_context(self):
        first = document([span()])
        for field in ('span', 'resource', 'scope', 'resource_schema', 'scope_schema'):
            second = deepcopy(first)
            rs = second['resourceSpans'][0]
            ss = rs['scopeSpans'][0]
            if field == 'span':
                ss['spans'][0]['attributes'] = [{'key': 'ignored', 'value': {'stringValue': 'changed'}}]
            elif field == 'resource':
                rs['resource']['attributes'].append({'key': 'ignored', 'value': {'stringValue': 'changed'}})
            elif field == 'scope':
                ss['scope']['version'] = 'changed'
            elif field == 'resource_schema':
                rs['schemaUrl'] = 'changed'
            else:
                ss['schemaUrl'] = 'changed'
            with self.subTest(field=field):
                result = convert(jsonl(first, second, document([span(2)])), Config(input_format='jsonl'))
                self.assertIsNone(result.prometheus)
                self.assertTrue(result.report['record_counts_complete'])
                self.assertEqual([result.report[k] for k in ('input_records', 'unique_records', 'rejected_records')], [3, 2, 1])
                self.assertIn('line 2', result.report['diagnostics'][0])
                self.assertIn('conflicting duplicate', result.report['diagnostics'][0])

    def test_malformed_interior_lines_reject_prefix_and_mark_counts_incomplete(self):
        first, last = encode(document([span()])), encode(document([span(2)]))
        for bad in (b'{', b'\xff', b'', b'  ', b'[]', b'{}', b'{"resourceSpans":null}',
                    b'{"resourceSpans":[],"resourceSpans":[]}', b'{"resourceSpans":[NaN]}',
                    b'{"resourceSpans":[{"scopeSpans":{}}]}'):
            with self.subTest(bad=bad):
                result = convert(b'\n'.join((first, bad, last)), Config(input_format='jsonl'))
                self.assertIsNone(result.prometheus)
                self.assertFalse(result.report['record_counts_complete'])
                self.assertEqual(result.report['unique_records'], 1)
                self.assertIn('line 2', result.report['diagnostics'][0])
                self.assertNotIn('prometheus_sha256', result.report)

    def test_line_byte_limit_counts_utf8_bytes_excludes_lf_or_crlf(self):
        line = encode(document([span(name='☃')])).replace(b'\\u2603', '☃'.encode())
        config = Config(input_format='jsonl', max_line_bytes=len(line))
        for ending in (b'', b'\n', b'\r\n'):
            with self.subTest(ending=ending):
                self.assertEqual(convert(line + ending, config).report['status'], 'ok')
                result = convert(jsonl(document([])) + line + ending, replace(config, max_line_bytes=len(line) - 1))
                self.assertIsNone(result.prometheus)
                self.assertFalse(result.report['record_counts_complete'])
                self.assertIn('line 2', result.report['diagnostics'][0])
                self.assertIn('line byte cap', result.report['diagnostics'][0])

    def test_global_span_total_byte_and_series_limits(self):
        data = jsonl(document([span()]), document([span()]), document([span(2, name='other')]))
        base = Config(input_format='jsonl', max_spans=3, max_input_bytes=len(data), max_series=16)
        self.assertEqual(convert(data, base).report['status'], 'ok')
        for key, value, diagnostic in (('max_spans', 2, 'span cap'), ('max_input_bytes', len(data) - 1, 'byte cap'),
                                       ('max_series', 15, 'series cap')):
            with self.subTest(key=key):
                result = convert(data, replace(base, **{key: value}))
                self.assertIsNone(result.prometheus)
                self.assertIn(diagnostic, result.report['diagnostics'][0])
                self.assertEqual(result.report['record_counts_complete'], key == 'max_series')

    def test_empty_stream_rejected_but_explicit_empty_request_accepted(self):
        config = Config(input_format='jsonl')
        self.assertIsNone(convert(b'', config).prometheus)
        result = convert(b'{"resourceSpans":[]}\r\n', config)
        self.assertEqual(result.report['status'], 'ok')
        self.assertEqual(result.report['input_records'], 0)
        # JSON remains the default: never guess format from content or extension.
        self.assertIsNone(convert(jsonl(document([]), document([]))).prometheus)

    def test_comparison_has_identical_attribution_and_suppresses_on_interior_error(self):
        doc = json.loads((ROOT / 'examples/spanmetrics/dimensions.json').read_text())
        from .test_comparison import BASELINE, CANDIDATE
        configs = [replace(config, input_format='jsonl') for config in (BASELINE, CANDIDATE)]
        data = jsonl(*split_requests(doc))
        result, single = compare(data, *configs), compare(encode(doc), BASELINE, CANDIDATE)
        for key in single.keys() - {'input_sha256', 'input_format'}:
            self.assertEqual(result[key], single[key], key)
        broken = compare(data + b'{\n' + data, *configs)
        self.assertFalse(broken['comparison_complete'])
        self.assertIsNone(broken['series_delta'])
        self.assertEqual(broken['partitions'], [])

    def test_library_validates_format_and_line_limit_and_shared_comparison_settings(self):
        for changes in ({'input_format': 'auto'}, {'max_line_bytes': 0}, {'max_line_bytes': True}):
            with self.subTest(changes=changes), self.assertRaises(InputError):
                Config(**changes)
        for changes in ({'input_format': 'jsonl'}, {'max_line_bytes': 100}):
            with self.subTest(changes=changes), self.assertRaises(InputError):
                compare(b'{"resourceSpans":[]}', Config(), replace(Config(), **changes))


class JsonlCliTests(unittest.TestCase):
    setUp = test_cli.CliTests.setUp
    run_cli = test_cli.CliTests.run_cli
    run_compare = test_cli.ComparisonCliTests.run_compare

    def test_installed_cli_saves_equivalent_metrics_and_complete_report(self):
        proc = self.run_cli('--input-format', 'jsonl', source=ROOT / 'examples/spanmetrics/traces.jsonl')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result, json.loads(self.report.read_text()))
        self.assertTrue(result['metrics_emitted'])
        self.assertEqual(result['unique_records'], 2)
        self.assertEqual(self.output.read_text(), convert((ROOT / 'examples/spanmetrics/traces.json').read_bytes()).prometheus)

    def test_interior_failure_and_limits_never_publish_partial_metrics(self):
        lines = (ROOT / 'examples/spanmetrics/traces.jsonl').read_bytes().splitlines()
        source = self.directory / 'capture.jsonl'
        conflict = json.loads(lines[0])
        conflict['resourceSpans'][0]['scopeSpans'][0]['spans'][0]['name'] = 'changed'
        cases = [(b'\n'.join((lines[0], b'{', lines[1])), ()),
                 (b'\n'.join((lines[0], encode(conflict), lines[1])), ()),
                 (b'\n'.join(lines), ('--max-spans', '1')),
                 (b'\n'.join(lines), ('--max-series', '8')),
                 (b'\n'.join(lines), ('--max-line-bytes', str(len(lines[0])))),
                 (b'\n'.join(lines), ('--max-input-bytes', '10'))]
        for data, options in cases:
            with self.subTest(options=options, data=data[:40]):
                source.write_bytes(data)
                for existing in (False, True):
                    self.output.unlink(missing_ok=True)
                    self.report.unlink(missing_ok=True)
                    if existing:
                        self.output.write_text('previous metrics\n')
                    proc = self.run_cli('--input-format', 'jsonl', '--force', *options, source=source)
                    self.assertEqual(proc.returncode, 1, proc.stderr)
                    report = json.loads(self.report.read_text())
                    self.assertFalse(report['metrics_emitted'])
                    self.assertEqual(report['status'], 'rejected')
                    self.assertEqual(self.output.exists(), existing)
                    if existing:
                        self.assertEqual(self.output.read_text(), 'previous metrics\n')
                    self.assertFalse(list(self.directory.glob('.spanmetrics-*')))

    def test_jsonl_compare_dry_run_and_rejection(self):
        source = self.directory / 'dimensions.jsonl'
        source.write_bytes(jsonl(*split_requests(json.loads((ROOT / 'examples/spanmetrics/dimensions.json').read_text()))))
        proc = self.run_compare('--input-format', 'jsonl', '--dry-run', source=source)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['series_delta'], 24)
        self.assertFalse(self.report.exists())
        source.write_bytes(source.read_bytes() + b'{\n')
        proc = self.run_compare('--input-format', 'jsonl', source=source)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = json.loads(self.report.read_text())
        self.assertFalse(report['comparison_complete'])
        self.assertIsNone(report['series_delta'])

    def test_bad_format_and_line_limit_options_are_usage_errors(self):
        for options in (('--input-format', 'auto'), ('--max-line-bytes', '0')):
            proc = self.run_cli(*options)
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(self.output.exists())
            self.assertFalse(self.report.exists())


if __name__ == '__main__':
    unittest.main()
