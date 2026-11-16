"""Synthetic wire fixtures for opt-in classic composite consistency checks."""
import json
from contextlib import redirect_stderr, redirect_stdout
from decimal import Inexact, InvalidOperation, ROUND_DOWN, getcontext, localcontext
from io import StringIO
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from prometheus_client import CollectorRegistry, Histogram, Summary, generate_latest

from otelwrench.promdiff.compare import compare
from otelwrench.promdiff.cli import main
from otelwrench.promdiff.parser import InputError, parse_snapshot


VALID = ('# TYPE h histogram\n'
         'h_bucket{le="1"} 1\n'
         'h_bucket{le="+Inf"} 2\n'
         'h_sum 1.5\n'
         'h_count 2\n')

SUMMARY_DUPLICATE = ('# TYPE s summary\n'
                     's{quantile="0.5"} 1\n'
                     's{quantile="0.50"} 2\n'
                     's_sum 3\n'
                     's_count 2\n')


class CompositeValidationTests(unittest.TestCase):
    def parse(self, text):
        return parse_snapshot(text, "fixture.prom", validate_composites=True)

    def assert_invalid(self, text, line, message, family="h"):
        with self.assertRaises(InputError) as context:
            self.parse(text)
        self.assertIn(f"fixture.prom:{line}: {family}:", str(context.exception))
        self.assertIn(message, str(context.exception))

    def test_valid_value_changes_still_compare_and_values_are_discarded(self):
        before = self.parse(VALID)
        after = self.parse(VALID.replace('} 1\n', '} 2\n').replace('} 2\n', '} 3\n')
                           .replace('h_sum 1.5', 'h_sum -Inf').replace('h_count 2', 'h_count 3'))
        self.assertEqual(compare(before, after)["changes"], [])
        self.assertEqual(before, parse_snapshot(VALID))
        self.assertEqual(set(vars(before.families["h"])),
                         {"name", "type", "help", "unit", "schemas", "series"})

    def test_validation_is_opt_in(self):
        invalid = VALID.replace('} 1\n', '} 3\n')
        self.assertEqual(compare(parse_snapshot(invalid), parse_snapshot(invalid))["status"], "pass")
        self.assert_invalid(invalid, 3, "cumulative bucket count decreases (previous bucket at line 2)")

    def test_bucket_boundaries_must_increase_in_wire_order(self):
        for first, second in [('2', '1'), ('1', '1.0'), ('+Inf', '1'),
                              ('9007199254740993', '9007199254740992')]:
            with self.subTest(first=first, second=second):
                text = ('# TYPE h histogram\n'
                        f'h_bucket{{le="{first}"}} 1\n'
                        f'h_bucket{{le="{second}"}} 1\n'
                        + ('h_bucket{le="+Inf"} 2\n' if first != '+Inf' else '')
                        + 'h_sum 1.5\nh_count ' + ('2\n' if first != '+Inf' else '1\n'))
                self.assert_invalid(text, 3, "bucket boundaries must be strictly increasing")

    def test_finite_nonnegative_integral_bucket_and_count_values(self):
        for value in ['-1', '0.5', 'NaN', '+Inf', '-Inf', '1.00000000000000001', '1e-9999']:
            for target, line in [('h_count 2', 5), ('} 1', 2)]:
                with self.subTest(value=value, target=target):
                    self.assert_invalid(VALID.replace(target, target.rsplit(' ', 1)[0] + ' ' + value),
                                        line, "must be a finite nonnegative integer")

    def test_terminal_count_relationship(self):
        self.assert_invalid(VALID.replace('h_count 2', 'h_count 3'), 3,
                            'le="+Inf" bucket must equal _count at line 5')

    def test_decimal_counts_are_not_rounded_through_float(self):
        text = VALID.replace('} 2\n', '} 9007199254740992\n').replace('h_count 2', 'h_count 9007199254740993')
        self.assert_invalid(text, 3, 'le="+Inf" bucket must equal _count')
        self.parse(VALID.replace('} 2\n', '} 2e1\n').replace('h_count 2', 'h_count 20.0'))
        self.parse(VALID.replace('h_bucket{le="1"} 1\n',
                                'h_bucket{le="9007199254740992"} 1\n'
                                'h_bucket{le="9007199254740993"} 1\n'))

    def test_extreme_exponents_produce_input_errors(self):
        for text, line in [(VALID.replace('h_count 2', 'h_count 1e99999999999999999999'), 5),
                           (VALID.replace('le="1"', 'le="1e99999999999999999999"'), 2)]:
            self.assert_invalid(text, line, 'numeric exponent outside supported decimal range')

    def test_composite_exponent_errors_do_not_depend_on_caller_decimal_traps(self):
        for value in ['1e99999999999999999999', '1e-99999999999999999999',
                      '-1e99999999999999999999', '-1e-99999999999999999999']:
            summary = f'# TYPE s summary\ns{{quantile="{value}"}} 1\ns_sum 1\ns_count 1\n'
            cases = [(VALID.replace('le="1"', f'le="{value}"'), 2, 'h'),
                     (VALID.replace('} 1\n', '} ' + value + '\n'), 2, 'h'),
                     (VALID.replace('h_count 2', 'h_count ' + value), 5, 'h'),
                     (summary, 2, 's'),
                     (f'# TYPE s summary\ns_sum 1\ns_count {value}\n', 3, 's')]
            for text, line, family in cases:
                for trap in [True, False]:
                    with self.subTest(value=value, line=line, family=family, trap=trap, text=text):
                        with localcontext() as ctx:
                            ctx.traps[InvalidOperation] = trap
                            with self.assertRaises(InputError) as error:
                                self.parse(text)
                            self.assertEqual(str(error.exception),
                                             f'fixture.prom:{line}: {family}: '
                                             'numeric exponent outside supported decimal range')

    def test_composite_parsing_preserves_caller_decimal_context(self):
        valid_summary = ('# TYPE s summary\ns{quantile="-0.0"} NaN\n'
                         's{quantile="0.50000000000000001"} 1\n'
                         's{quantile="0.5"} 1\ns{quantile="1"} 1\ns_sum NaN\ns_count 0\n')
        for trap in [True, False]:
            for flagged in [True, False]:
                with self.subTest(trap=trap, flagged=flagged), localcontext() as ctx:
                    ctx.prec, ctx.rounding, ctx.Emin, ctx.Emax, ctx.clamp = 2, ROUND_DOWN, -2, 2, 1
                    ctx.traps[InvalidOperation] = trap
                    ctx.clear_flags()
                    ctx.flags[Inexact] = flagged
                    before = str(ctx)
                    for text in [VALID, valid_summary, VALID.replace('le="1"', 'le="1e9999"')]:
                        self.assertEqual(self.parse(text), parse_snapshot(text))
                        self.assertIs(getcontext(), ctx)
                        self.assertEqual(str(ctx), before)
                    # Failed conversions must not set caller flags, whether or
                    # not the caller would trap InvalidOperation itself.
                    for text in [VALID.replace('le="1"', 'le="1e99999999999999999999"'),
                                 valid_summary.replace('"-0.0"', '"1e-99999999999999999999"')]:
                        with self.assertRaises(InputError):
                            self.parse(text)
                        self.assertIs(getcontext(), ctx)
                        self.assertEqual(str(ctx), before)

    def test_cli_exponent_errors_preserve_nondefault_decimal_context(self):
        with tempfile.TemporaryDirectory() as directory:
            good, bad = (Path(directory) / name for name in ['good.prom', 'bad.prom'])
            good.write_text(VALID)
            for value in ['1e99999999999999999999', '1e-99999999999999999999']:
                cases = [(VALID.replace('le="1"', f'le="{value}"'), 2, 'h'),
                         (VALID.replace('h_count 2', 'h_count ' + value), 5, 'h'),
                         (f'# TYPE s summary\ns{{quantile="{value}"}} 1\ns_sum 1\ns_count 1\n', 2, 's')]
                for text, line, family in cases:
                    bad.write_text(text)
                    for fmt in ['json', 'text']:
                        for left, right in [(bad, bad), (bad, good), (good, bad)]:
                            with self.subTest(value=value, family=family, line=line, fmt=fmt,
                                              left=left, right=right), localcontext() as ctx:
                                ctx.traps[InvalidOperation] = False
                                ctx.clear_flags()
                                before = str(ctx)
                                out, err = StringIO(), StringIO()
                                with redirect_stdout(out), redirect_stderr(err):
                                    code = main([str(left), str(right), '--format', fmt,
                                                 '--validate-composites'])
                                self.assertEqual(code, 2)
                                self.assertEqual(out.getvalue(), '')
                                expected = f'{bad}:{line}: {family}: numeric exponent outside supported decimal range'
                                if fmt == 'json':
                                    self.assertEqual(json.loads(err.getvalue()),
                                                     {'report_version': 1, 'status': 'error', 'error': expected})
                                else:
                                    self.assertEqual(err.getvalue(), f'otelwrench promdiff: {expected}\n')
                                self.assertEqual(str(ctx), before)
                    self.assertEqual(bad.read_text(), text)
            self.assertEqual(good.read_text(), VALID)

    def test_missing_required_samples_have_family_and_group_line(self):
        for fragment, message in [('h_sum 1.5\n', 'missing required h_sum'),
                                  ('h_count 2\n', 'missing required h_count'),
                                  ('h_bucket{le="+Inf"} 2\n', 'requires buckets including le="+Inf"')]:
            with self.subTest(fragment=fragment):
                self.assert_invalid(VALID.replace(fragment, ''), 2, message)
        self.assert_invalid('# TYPE h histogram\nh_sum 0\nh_count 0\n', 2,
                            'requires buckets including le="+Inf"')

    def test_groups_are_checked_independently_even_when_interleaved(self):
        text = ('# TYPE h histogram\n'
                'h_bucket{route="secret-a",le="1"} 1\n'
                'h_bucket{route="secret-b",le="1"} 3\n'
                'h_bucket{le="+Inf",route="secret-a"} 2\n'
                'h_bucket{le="+Inf",route="secret-b"} 4\n'
                'h_sum{route="secret-b"} 2\nh_count{route="secret-b"} 4\n'
                'h_sum{route="secret-a"} 1\nh_count{route="secret-a"} 2\n')
        self.parse(text)
        self.assert_invalid(text.replace('h_count{route="secret-b"} 4\n', ''), 3,
                            'missing required h_count')
        try:
            self.parse(text.replace('le="+Inf",route="secret-b"} 4', 'le="+Inf",route="secret-b"} 2'))
        except InputError as exc:
            self.assertIn('fixture.prom:5: h:', str(exc))
            self.assertNotIn('secret-b', str(exc))
        else:
            self.fail('decreasing bucket count accepted')

    def test_summary_count_and_required_samples_without_inferred_quantiles(self):
        text = '# TYPE s summary\ns_sum NaN\ns_count 0\n'
        self.parse(text)
        self.parse('# TYPE s summary\ns{quantile="0.5"} NaN\ns_sum NaN\ns_count 0\n')
        for value in ['-1', '1.5', 'NaN', 'Inf']:
            self.assert_invalid(text.replace('s_count 0', 's_count ' + value), 3,
                                'must be a finite nonnegative integer', family='s')
        self.assert_invalid('# TYPE s summary\ns{quantile="0.5"} 1\ns_sum 1\n', 2,
                            'missing required s_count', family='s')
        self.assert_invalid('# TYPE s summary\ns_count 1\n', 2,
                            'missing required s_sum', family='s')

    def test_summary_numerically_duplicate_quantiles_are_opt_in_errors(self):
        for first, second in [('0.5', '0.50'), ('0.5', '5e-1'), ('0', '-0.0'),
                              ('1', '+1e0'),
                              ('0.50000000000000001', '5.0000000000000001e-1')]:
            with self.subTest(first=first, second=second):
                text = SUMMARY_DUPLICATE.replace('"0.5"', f'"{first}"').replace('"0.50"', f'"{second}"')
                self.assertEqual(compare(parse_snapshot(text), parse_snapshot(text))['changes'], [])
                self.assert_invalid(text, 3,
                                    'numerically duplicate quantile (previous quantile at line 2)', family='s')

    def test_summary_duplicate_quantiles_need_not_be_adjacent(self):
        text = SUMMARY_DUPLICATE.replace('s{quantile="0.50"}',
                                         's{quantile="0.9"} 2\ns{quantile="0.50"}')
        self.assert_invalid(text, 4, 'numerically duplicate quantile (previous quantile at line 2)', family='s')

    def test_summary_quantile_uniqueness_is_scoped_to_each_base_label_group_and_family(self):
        text = ('# TYPE s summary\n'
                's{route="secret-a",quantile="0.5"} NaN\n'
                's{quantile="0.50",route="secret-b"} 2\n'
                's{route="secret-a",quantile="0.9"} 3\n'
                's{route="secret-b",quantile="9e-1"} 4\n'
                's_sum{route="secret-b"} 6\ns_count{route="secret-b"} 2\n'
                's_sum{route="secret-a"} 3\ns_count{route="secret-a"} 2\n'
                's_sum{route="no-quantiles"} NaN\ns_count{route="no-quantiles"} 0\n'
                '# TYPE t summary\nt{quantile="0.50"} 1\nt_sum 1\nt_count 1\n')
        self.assertEqual(self.parse(text).summary(), {'families': 2, 'observed_series': 13})
        bad = text.replace('s_sum{route="secret-b"}',
                           's{route="secret-b",quantile=".500"} 7\ns_sum{route="secret-b"}')
        with self.assertRaises(InputError) as context:
            self.parse(bad)
        self.assertEqual(str(context.exception),
                         'fixture.prom:6: s: numerically duplicate quantile (previous quantile at line 3)')

    def test_distinct_summary_quantiles_keep_exact_wire_identity_and_need_no_order(self):
        text = ('# TYPE s summary\n'
                's{quantile="1"} NaN\ns{quantile="0.50000000000000001"} -Inf\n'
                's{quantile="0.5"} +Inf\ns{quantile="0"} -1\n'
                's_sum NaN\ns_count 0\n')
        snapshot = self.parse(text)
        self.assertEqual(snapshot, parse_snapshot(text))
        labels = {dict(labels)['quantile'] for name, labels in snapshot.series if name == 's'}
        self.assertEqual(labels, {'1', '0.50000000000000001', '0.5', '0'})
        changed_values = self.parse(text.replace('} NaN', '} 100').replace('} -1', '} 2'))
        self.assertEqual(compare(snapshot, changed_values)['changes'], [])

    def test_summary_quantile_extreme_exponent_has_input_diagnostic(self):
        text = SUMMARY_DUPLICATE.replace('"0.5"', '"1e-99999999999999999999"')
        self.assert_invalid(text, 2, 'numeric exponent outside supported decimal range', family='s')

    def test_summary_quantile_unsupported_exponent_diagnostic_precedes_float_screen(self):
        for label in ['1e99999999999999999999', '1e-99999999999999999999',
                      '-1e99999999999999999999', '-1e-99999999999999999999']:
            with self.subTest(label=label):
                text = f'# TYPE s summary\ns{{quantile="{label}"}} 1\ns_sum 1\ns_count 1\n'
                with self.assertRaises(InputError) as context:
                    self.parse(text)
                self.assertEqual(str(context.exception),
                                 'fixture.prom:2: s: numeric exponent outside supported decimal range')
                if 'e-' in label:
                    default = parse_snapshot(text)
                    self.assertIn(('s', (('quantile', label),)), default.series)
                else:
                    with self.assertRaises(InputError) as context:
                        parse_snapshot(text, 'fixture.prom')
                    self.assertEqual(str(context.exception), 'fixture.prom:2: s: invalid quantile value')

    def test_summary_quantile_invalid_label_diagnostics_are_preserved(self):
        for label in ['NaN', 'Inf', '+Inf', '-Inf', '1.5', '-0.5', '1e9999', '-1e9999',
                      'bad', '1_0', 'nan']:
            for flag in [False, True]:
                with self.subTest(label=label, validate_composites=flag):
                    text = f'# TYPE s summary\ns{{quantile="{label}"}} 1\ns_sum 1\ns_count 1\n'
                    with self.assertRaises(InputError) as context:
                        parse_snapshot(text, 'fixture.prom', validate_composites=flag)
                    self.assertEqual(str(context.exception), 'fixture.prom:2: s: invalid quantile value')

    def test_cli_summary_quantile_unsupported_exponent_in_either_input(self):
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory) / 'good.prom'
            bad = Path(directory) / 'bad.prom'
            good.write_text('# TYPE s summary\ns_sum 1\ns_count 1\n')
            for label in ['1e99999999999999999999', '1e-99999999999999999999']:
                text = f'# TYPE s summary\ns{{quantile="{label}"}} 1\ns_sum 1\ns_count 1\n'
                bad.write_text(text)
                for output_format in ['json', 'text']:
                    for left, right in [(bad, bad), (bad, good), (good, bad)]:
                        with self.subTest(label=label, output_format=output_format, left=left, right=right):
                            result = subprocess.run(
                                [sys.executable, '-m', 'otelwrench', 'promdiff', str(left), str(right),
                                 '--format', output_format, '--validate-composites'],
                                text=True, capture_output=True, timeout=60)
                            self.assertEqual(result.returncode, 2)
                            self.assertEqual(result.stdout, '')
                            expected = f'{bad}:2: s: numeric exponent outside supported decimal range'
                            if output_format == 'json':
                                self.assertEqual(json.loads(result.stderr),
                                                 {'report_version': 1, 'status': 'error', 'error': expected})
                            else:
                                self.assertEqual(result.stderr, f'otelwrench promdiff: {expected}\n')
                self.assertEqual(bad.read_text(), text)
            self.assertEqual(good.read_text(), '# TYPE s summary\ns_sum 1\ns_count 1\n')

    def test_exact_summary_quantile_range_rejects_float_rounded_outliers(self):
        for label in ['1.00000000000000001', '-1e-9999']:
            with self.subTest(label=label):
                text = f'# TYPE s summary\ns{{quantile="{label}"}} 1\ns_sum 1\ns_count 1\n'
                default = parse_snapshot(text)
                self.assertEqual(compare(default, default)['changes'], [])
                with self.assertRaises(InputError) as context:
                    self.parse(text)
                self.assertEqual(str(context.exception),
                                 'fixture.prom:2: s: invalid quantile value (exact label outside [0,1])')

    def test_exact_summary_quantile_range_accepts_endpoints_and_precise_values(self):
        # Each spelling is a separate fixture: numerically equal signed zeros
        # in the same group must still be rejected by the uniqueness check.
        for label in ['0', '1', '-0.0', '+0e-9999', '-0e+9999', '+1e0',
                      '1e-9999', '0.9999999999999999999999999999999999']:
            with self.subTest(label=label):
                text = f'# TYPE s summary\ns{{quantile="{label}"}} NaN\ns_sum NaN\ns_count 0\n'
                snapshot = self.parse(text)
                self.assertEqual(snapshot, parse_snapshot(text))
                self.assertIn(('s', (('quantile', label),)), snapshot.series)

    def test_cli_exact_summary_quantile_range_validates_either_input(self):
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory) / 'good.prom'
            bad = Path(directory) / 'bad.prom'
            good.write_text('# TYPE s summary\ns_sum 1\ns_count 1\n')
            for label in ['1.00000000000000001', '-1e-9999']:
                text = f'# TYPE s summary\ns{{quantile="{label}"}} 1\ns_sum 1\ns_count 1\n'
                bad.write_text(text)
                for output_format in ['json', 'text']:
                    command = [sys.executable, '-m', 'otelwrench', 'promdiff']
                    def run(left, right, *flags):
                        return subprocess.run(command + [str(left), str(right), '--format', output_format, *flags],
                                              text=True, capture_output=True, timeout=60)
                    default = run(bad, bad)
                    self.assertEqual(default.returncode, 0, default.stderr)
                    self.assertEqual(default.stderr, '')
                    for left, right in [(bad, bad), (bad, good), (good, bad)]:
                        with self.subTest(label=label, output_format=output_format, left=left, right=right):
                            result = run(left, right, '--validate-composites')
                            self.assertEqual(result.returncode, 2, result.stdout)
                            self.assertEqual(result.stdout, '')
                            expected = f'{bad}:2: s: invalid quantile value (exact label outside [0,1])'
                            if output_format == 'json':
                                self.assertEqual(json.loads(result.stderr),
                                                 {'report_version': 1, 'status': 'error', 'error': expected})
                            else:
                                self.assertEqual(result.stderr, f'otelwrench promdiff: {expected}\n')
                self.assertEqual(bad.read_text(), text)
            self.assertEqual(good.read_text(), '# TYPE s summary\ns_sum 1\ns_count 1\n')

    def test_metadata_only_and_untyped_components_are_not_inferred(self):
        self.assertEqual(self.parse('# TYPE h histogram\n# TYPE s summary\n').summary(),
                         {"families": 2, "observed_series": 0})
        self.parse('h_bucket{le="1"} -1\nh_count NaN\n')
        self.parse(VALID.replace('le="1"', 'le="-Inf"').replace('} 1\n', '} -0\n'))

    def test_real_pinned_client_output(self):
        registry = CollectorRegistry()
        histogram = Histogram('duration', 'Duration.', ['route'], buckets=[0.1, 1], registry=registry)
        histogram.labels('a').observe(0.3)
        histogram.labels('b')  # Real zero-observation group, with explicitly exported zeros.
        Summary('payload', 'Payload.', registry=registry).observe(10)
        snapshot = self.parse(generate_latest(registry).decode())
        self.assertEqual(len(snapshot.families['duration'].series), 10)
        self.assertEqual(len(snapshot.families['payload'].series), 2)

    def test_cli_validates_both_inputs_before_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory) / 'good.prom'
            bad = Path(directory) / 'bad.prom'
            good.write_text(VALID)
            bad.write_text(VALID.replace('h_count 2', 'h_count 3'))
            command = [sys.executable, '-m', 'otelwrench', 'promdiff']
            def run(left, right, *flags):
                return subprocess.run(command + [str(left), str(right), '--format', 'json', *flags],
                                      text=True, capture_output=True, timeout=60)
            self.assertEqual(run(good, bad).returncode, 0)
            success = run(good, good, '--validate-composites')
            self.assertEqual(success.returncode, 0, success.stderr)
            self.assertEqual(json.loads(success.stdout)['changes'], [])
            self.assertEqual(success.stdout, run(good, good).stdout)
            for left, right in [(good, bad), (bad, good)]:
                result = run(left, right, '--validate-composites')
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                error = json.loads(result.stderr)
                self.assertEqual(error['status'], 'error')
                self.assertIn(f'{bad}:3: h:', error['error'])
                self.assertIn('_count at line 5', error['error'])

    def test_cli_rejects_duplicate_summary_quantiles_in_either_input(self):
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory) / 'no-quantiles.prom'
            bad = Path(directory) / 'duplicate.prom'
            good.write_text('# TYPE s summary\ns_sum NaN\ns_count 0\n')
            bad.write_text(SUMMARY_DUPLICATE)
            command = [sys.executable, '-m', 'otelwrench', 'promdiff']
            for output_format in ['json', 'text']:
                with self.subTest(output_format=output_format):
                    def run(left, right, *flags):
                        return subprocess.run(command + [str(left), str(right), '--format', output_format, *flags],
                                              text=True, capture_output=True, timeout=60)
                    default = run(bad, bad)
                    self.assertEqual(default.returncode, 0, default.stderr)
                    success = run(good, good, '--validate-composites')
                    self.assertEqual(success.returncode, 0, success.stderr)
                    self.assertEqual(success.stdout, run(good, good).stdout)
                    for left, right in [(good, bad), (bad, good)]:
                        result = run(left, right, '--validate-composites')
                        self.assertEqual(result.returncode, 2)
                        self.assertEqual(result.stdout, '')
                        expected = f'{bad}:3: s: numerically duplicate quantile (previous quantile at line 2)'
                        if output_format == 'json':
                            self.assertEqual(json.loads(result.stderr),
                                             {'report_version': 1, 'status': 'error', 'error': expected})
                        else:
                            self.assertEqual(result.stderr, f'otelwrench promdiff: {expected}\n')
            self.assertEqual(bad.read_text(), SUMMARY_DUPLICATE)
            self.assertEqual(good.read_text(), '# TYPE s summary\ns_sum NaN\ns_count 0\n')


if __name__ == '__main__':
    unittest.main()
