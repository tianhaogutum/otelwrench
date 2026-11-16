# SPDX-License-Identifier: Apache-2.0
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from otelwrench.promnames import ConversionError, bundle
from otelwrench.promnames.core import WITH, WITHOUT
from .helpers import CLI, ROOT, attr, document, gauge, sum_metric


class InspectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.output = self.directory / 'result.zip'
        self.source = (ROOT / 'examples/prom-names/milliseconds.json').read_bytes()

    def create(self, source=None, **config):
        members, mapping = bundle.build_bundle(self.source if source is None else source, **config)
        bundle.atomic_write_bundle(self.output, members)
        return members, mapping

    def run_cli(self, path=None, *, source=None, metric_names=None):
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        args = [*CLI, 'inspect', str(path or self.output)]
        if source is not None:
            args += ['--input', str(source)]
        if metric_names is not None:
            for name in metric_names:
                args += ['--metric', name]
        return subprocess.run(args, cwd=self.directory,
                              env=env, capture_output=True, text=True, timeout=60)

    def assert_rejected(self, contains=None, *, source=None, metric_names=None):
        before = {p.name: p.read_bytes() for p in self.directory.iterdir() if p.is_file()}
        result = self.run_cli(source=source, metric_names=metric_names)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(result.stdout, '')
        error = json.loads(result.stderr)
        self.assertEqual(error['status'], 'error')
        self.assertNotIn('Traceback', result.stderr)
        if contains:
            self.assertIn(contains, error['errors'][0])
        self.assertEqual({p.name: p.read_bytes() for p in self.directory.iterdir() if p.is_file()}, before)

    def test_selected_cli_summary_counts_names_losses_and_read_only_determinism(self):
        _, mapping = self.create(convert_unit='ms:s', convert_metrics=['latency'])
        before = self.output.read_bytes()
        first = self.run_cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stderr, '')
        summary = json.loads(first.stdout)
        self.assertEqual(set(summary), {'schema_version', 'status', 'verified', 'reproduced', 'configuration',
                                       'sample_count', 'metric_count', 'loss_count', 'losses', 'metrics'})
        self.assertEqual((summary['schema_version'], summary['status'], summary['verified']), (1, 'ok', True))
        self.assertIs(summary['reproduced'], False)
        self.assertEqual((summary['sample_count'], summary['metric_count'], summary['loss_count']), (6, 5, 2))
        self.assertEqual(summary['configuration'], {'strategy': 'UnderscoreEscapingWithSuffixes',
            'allow_nonfinite': False, 'promote_resource': [], 'convert_unit': 'ms:s', 'convert_metrics': ['latency']})
        self.assertEqual(summary['losses'], mapping['losses'])
        names = [(m['original_name'], m['target_family'], m['original_unit'], m['target_unit'],
                  m['sample_count'], m['unit_converted']) for m in summary['metrics']]
        self.assertEqual(names, [('latency', 'latency_seconds', 'ms', 's', 2, True),
            ('elapsed_milliseconds_total', 'elapsed_milliseconds_total', 'ms', 'ms', 1, False),
            ('payload', 'payload_bytes', 'By', 'By', 1, False),
            ('ready', 'ready_ratio', '1', '1', 1, False), ('uptime', 'uptime_seconds', 's', 's', 1, False)])
        self.assertEqual(summary['metrics'][1]['parser_family'], 'elapsed_milliseconds')
        self.assertEqual(summary['metrics'][1]['parser_sample'], 'elapsed_milliseconds_total')
        self.assertEqual(summary['metrics'][1]['target_type'], 'counter')
        self.assertEqual(summary['metrics'][0]['path'], '$.resourceMetrics[0].scopeMetrics[0].metrics[0]')
        self.assertEqual(first.stdout, self.run_cli().stdout)
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(list(self.directory.iterdir()), [self.output])

    def test_legacy_all_ms_default_and_no_suffix_counter_names(self):
        for config in ({}, {'convert_unit': 'ms:s'}, {'strategy': WITHOUT}):
            with self.subTest(config=config):
                _, mapping = self.create(**config)
                result = bundle.inspect_bundle(self.output)
                self.assertNotIn('convert_metrics', result['configuration'])
                self.assertEqual(result['sample_count'], 6)
                self.assertEqual(result['losses'], mapping['losses'])
                self.assertEqual([m['unit_converted'] for m in result['metrics']],
                                 [config.get('convert_unit') == 'ms:s'] * 2 + [False] * 3)
        self.create(json.dumps(document(sum_metric('requests'))).encode(), strategy=WITHOUT)
        metric = bundle.inspect_bundle(self.output)['metrics'][0]
        self.assertEqual((metric['target_family'], metric['parser_family'], metric['parser_sample']),
                         ('requests', 'requests', 'requests_total'))

    def test_empty_and_repeated_descriptors_are_counted_separately(self):
        self.create(b'{}')
        result = bundle.inspect_bundle(self.output)
        self.assertEqual((result['sample_count'], result['metric_count'], result['loss_count']), (0, 0, 0))
        source = {'resourceMetrics': [{'scopeMetrics': [{'metrics': [gauge('wait.µ', 'ms', attrs=[attr('site', 'a')])]},
            {'metrics': [gauge('wait.µ', 'ms', attrs=[attr('site', 'b')]),
                         {'name': 'empty', 'unit': 'ms', 'gauge': {}}]}]}]}
        self.create(json.dumps(source).encode(), convert_unit='ms:s', convert_metrics=['wait.µ', 'empty'])
        result = bundle.inspect_bundle(self.output)
        self.assertEqual((result['sample_count'], result['metric_count']), (2, 3))
        self.assertEqual([m['sample_count'] for m in result['metrics']], [1, 1, 0])
        self.assertEqual([m['original_name'] for m in result['metrics']], ['wait.µ', 'wait.µ', 'empty'])
        self.assertEqual(len({m['path'] for m in result['metrics']}), 3)
        self.assertTrue(all(m['unit_converted'] for m in result['metrics']))
        self.assertEqual(self.run_cli().returncode, 0)

    def test_hash_verification_is_mandatory_and_precedes_mapping_decode(self):
        members, _ = self.create()
        for name in bundle.ARTIFACTS:
            for changed in (b'!' + members[name][1:], members[name] + b'\n'):
                with self.subTest(name=name, same_size=len(changed) == len(members[name])):
                    bundle.atomic_write_bundle(self.output, {**members, name: changed})
                    self.assert_rejected(f'{name}: SHA-256/size mismatch')

    def test_malformed_archives_manifests_and_missing_files_fail_without_writes(self):
        members, _ = self.create()
        manifest = json.loads(members['manifest.json'])
        cases = [{**members, 'manifest.json': b'{'},
                 {**members, 'manifest.json': json.dumps({**manifest, 'schema_version': 2}).encode()},
                 {**members, 'manifest.json': b'{"schema_version":1,"schema_version":1}'},
                 {k: v for k, v in members.items() if k != 'mapping.json'},
                 {**members, '../extra': b'never extract me'}]
        for changed in cases:
            with self.subTest(names=list(changed)):
                with zipfile.ZipFile(self.output, 'w') as archive:
                    for name, data in changed.items():
                        archive.writestr(name, data)
                self.assert_rejected()
        bundle.atomic_write_bundle(self.output, members)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(self.output, 'a') as archive:
                archive.writestr('mapping.json', members['mapping.json'])
        self.assert_rejected()
        with zipfile.ZipFile(self.output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        self.assert_rejected('uncompressed')
        for raw in (b'not a zip', self.output.read_bytes()[:40]):
            self.output.write_bytes(raw)
            self.assert_rejected('invalid bundle')
        self.output.unlink()
        self.assert_rejected()
        self.assertEqual(self.run_cli(self.directory).returncode, 2)

    def test_hash_valid_but_unusable_mapping_fails_cleanly(self):
        members, mapping = self.create(convert_unit='ms:s', convert_metrics=['latency'])
        cases = [b'{', b'[]', b'\xff', b'{"a":1,"a":2}', b'{"a":NaN}']
        for key, value in [('schema_version', True), ('schema_version', 2), ('status', 'error'),
                           ('sample_count', True), ('sample_count', -1), ('sample_count', 7),
                           ('metrics', {}), ('losses', [None]), ('losses', [{'path': 'x', 'reason': 2}]),
                           ('strategy', WITHOUT), ('unit_conversion', None), ('resource_promotion', ['other']),
                           ('unit_conversion_metrics', ['other'])]:
            cases.append(bundle.json_bytes({**mapping, key: value}))
        for key, value in [('original_name', None), ('target_family', 2), ('target_type', 'histogram'),
                           ('parser_sample', '\ud800'), ('target_unit', []), ('data_points', {}),
                           ('original_unit', 'us'), ('target_unit', 'ms')]:
            changed = copy.deepcopy(mapping)
            changed['metrics'][0][key] = value
            cases.append(json.dumps(changed).encode())
        missing = copy.deepcopy(mapping)
        del missing['metrics'][0]['path']
        cases.append(bundle.json_bytes(missing))
        for raw in cases:
            with self.subTest(raw=raw[:100]):
                manifest = json.loads(members['manifest.json'])
                manifest['artifacts']['mapping.json'] = bundle.digest(raw)
                bundle.atomic_write_bundle(self.output, {**members, 'mapping.json': raw,
                                                         'manifest.json': bundle.json_bytes(manifest)})
                self.assert_rejected('mapping')

    def test_inspection_uses_verified_bytes_during_concurrent_replacement(self):
        self.create(b'{}')
        replacement, _ = bundle.build_bundle(self.source, convert_unit='ms:s', convert_metrics=['latency'])
        real_read = zipfile.ZipFile.read

        def replace_after_first_read(archive, name, *args, **kwargs):
            value = real_read(archive, name, *args, **kwargs)
            if name == 'exposition.prom':
                bundle.atomic_write_bundle(self.output, replacement)
            return value

        with patch.object(zipfile.ZipFile, 'read', replace_after_first_read):
            result = bundle.inspect_bundle(self.output)
        self.assertEqual((result['sample_count'], result['metrics']), (0, []))
        self.assertEqual(bundle.inspect_bundle(self.output)['sample_count'], 6)

    def test_selection_feasibility_rejects_absent_exact_raw_names(self):
        members, mapping = self.create(convert_unit='ms:s', convert_metrics=['latency'])
        for name in ('missing', 'Latency', 'latency_seconds', 'latency*', ' latency ', 'latency.µ'):
            for empty in (False, True):
                with self.subTest(name=name, empty=empty):
                    changed = copy.deepcopy(mapping)
                    names = sorted(['latency', name])
                    changed['unit_conversion_metrics'] = names
                    if empty:
                        changed['metrics'] = []
                        changed['sample_count'] = 0
                    raw = bundle.json_bytes(changed)
                    manifest = json.loads(members['manifest.json'])
                    manifest['configuration']['convert_metrics'] = names
                    manifest['artifacts']['mapping.json'] = bundle.digest(raw)
                    bundle.atomic_write_bundle(self.output, {**members, 'mapping.json': raw,
                        'manifest.json': bundle.json_bytes(manifest)})
                    # A coherent hash rewrite passes integrity but cannot describe a selection.
                    self.assertIs(bundle.verify_bundle(self.output)['reproduced'], False)
                    self.assert_rejected('mapping: unmatched exact raw metric names')
                    with self.assertRaisesRegex(ConversionError, 'unmatched exact raw metric names'):
                        bundle.inspect_bundle(self.output)

    def test_selection_feasibility_checks_every_repeated_and_empty_descriptor_unit(self):
        source = {'resourceMetrics': [
            {'scopeMetrics': [{'metrics': [gauge('wait.µ', 'ms', attrs=[attr('site', 'a')])]}]},
            {'scopeMetrics': [{'metrics': [gauge('wait.µ', 'ms', attrs=[attr('site', 'b')])]},
                              {'metrics': [{'name': 'wait.µ', 'unit': 'ms', 'gauge': {}}]}]}]}
        members, mapping = self.create(json.dumps(source).encode(),
                                       convert_unit='ms:s', convert_metrics=['wait.µ'])
        for index in range(3):
            for unit in ('', 's', 'By', '1'):
                with self.subTest(index=index, unit=unit):
                    changed = copy.deepcopy(mapping)
                    # Preserve the existing target-unit consistency rule, isolating
                    # the missing requirement that selected originals must be ms.
                    changed['metrics'][index]['original_unit'] = unit
                    changed['metrics'][index]['target_unit'] = unit
                    raw = bundle.json_bytes(changed)
                    manifest = json.loads(members['manifest.json'])
                    manifest['artifacts']['mapping.json'] = bundle.digest(raw)
                    bundle.atomic_write_bundle(self.output, {**members, 'mapping.json': raw,
                        'manifest.json': bundle.json_bytes(manifest)})
                    self.assertIs(bundle.verify_bundle(self.output)['reproduced'], False)
                    self.assert_rejected('mapping: selected metric')
                    with self.assertRaisesRegex(ConversionError, 'selected metric.*unit ms'):
                        bundle.inspect_bundle(self.output)

    def test_selected_naming_rejects_impossible_target_and_parser_names(self):
        counter = sum_metric('elapsed.milliseconds_total', 'ms')
        counter['sum']['dataPoints'][0]['attributes'] = [attr('site', 'a')]
        repeated = copy.deepcopy(counter)
        repeated['sum']['dataPoints'][0]['attributes'] = [attr('site', 'b')]
        empty = copy.deepcopy(counter)
        empty['sum']['dataPoints'] = []
        source = json.dumps({'resourceMetrics': [
            {'scopeMetrics': [{'metrics': [counter]}]},
            {'scopeMetrics': [{'metrics': [repeated]}, {'metrics': [empty]}]}]}).encode()
        for strategy in (WITH, WITHOUT):
            members, mapping = self.create(source, strategy=strategy, convert_unit='ms:s',
                                            convert_metrics=['elapsed.milliseconds_total'])
            for index in range(3):
                for field in ('target_family', 'parser_family', 'parser_sample', 'stale_names'):
                    with self.subTest(strategy=strategy, index=index, field=field):
                        changed = copy.deepcopy(mapping)
                        if field == 'stale_names':
                            changed['metrics'][index].update(target_family='elapsed_milliseconds_total',
                                parser_family='elapsed_milliseconds', parser_sample='elapsed_milliseconds_total')
                        else:
                            changed['metrics'][index][field] = 'unrelated_seconds'
                        raw = bundle.json_bytes(changed)
                        manifest = json.loads(members['manifest.json'])
                        manifest['artifacts']['mapping.json'] = bundle.digest(raw)
                        bundle.atomic_write_bundle(self.output, {**members, 'mapping.json': raw,
                            'manifest.json': bundle.json_bytes(manifest)})
                        self.assertIs(bundle.verify_bundle(self.output)['reproduced'], False)
                        self.assert_rejected('disagrees with configured conversion naming')
                        with self.assertRaisesRegex(ConversionError, 'bundle mapping: selected metric'):
                            bundle.inspect_bundle(self.output)
            # Exact selection names themselves must also permit a converted name.
            with self.subTest(strategy=strategy, invalid_raw_name='µ'):
                changed = copy.deepcopy(mapping)
                changed['unit_conversion_metrics'] = ['µ']
                for metric in changed['metrics']:
                    metric['original_name'] = 'µ'
                raw = bundle.json_bytes(changed)
                manifest = json.loads(members['manifest.json'])
                manifest['configuration']['convert_metrics'] = ['µ']
                manifest['artifacts']['mapping.json'] = bundle.digest(raw)
                bundle.atomic_write_bundle(self.output, {**members, 'mapping.json': raw,
                    'manifest.json': bundle.json_bytes(manifest)})
                self.assert_rejected('bundle mapping: selected metric')

    def repeated_type_source(self, kind, monotonic=None):
        metric = (gauge('latency', 'ms') if kind == 'gauge' else
                  sum_metric('latency', 'ms', monotonic=monotonic))
        if monotonic == 'omitted':
            del metric['sum']['isMonotonic']
        first, repeated, empty = (copy.deepcopy(metric) for _ in range(3))
        first[kind]['dataPoints'][0]['attributes'] = [attr('site', 'a')]
        repeated[kind]['dataPoints'][0]['attributes'] = [attr('site', 'b')]
        empty[kind]['dataPoints'] = []
        return json.dumps({'resourceMetrics': [
            {'scopeMetrics': [{'metrics': [first, gauge('payload', 'By')]}]},
            {'scopeMetrics': [{'metrics': [repeated]}, {'metrics': [empty]}]}]}).encode()

    def test_selected_types_reject_coherent_target_rewrites_at_every_occurrence(self):
        for kind, monotonic in (('gauge', None), ('sum', False), ('sum', True)):
            for strategy in (WITH, WITHOUT):
                members, mapping = self.create(self.repeated_type_source(kind, monotonic),
                    strategy=strategy, convert_unit='ms:s', convert_metrics=['latency'])
                target_type = 'gauge' if monotonic is True else 'counter'
                family = ('latency_seconds' if strategy == WITH else 'latency')
                target = family + ('_total' if target_type == 'counter' and strategy == WITH else '')
                sample = family + ('_total' if target_type == 'counter' else '')
                for index in (0, 2, 3):
                    with self.subTest(kind=kind, monotonic=monotonic, strategy=strategy, index=index):
                        changed = copy.deepcopy(mapping)
                        changed['metrics'][index].update(target_type=target_type, target_family=target,
                            parser_family=family, parser_sample=sample)
                        self.write_changed_mapping(members, changed)
                        self.assertIs(bundle.verify_bundle(self.output)['reproduced'], False)
                        with self.assertRaisesRegex(ConversionError, 'target_type disagrees with original'):
                            bundle.inspect_bundle(self.output)
                        # A filter hiding this selected descriptor must not bypass validation.
                        self.assert_rejected('target_type disagrees with original', metric_names=['payload'])

    def test_selected_types_reject_missing_or_malformed_original_semantics(self):
        missing = object()
        for kind, monotonic in (('gauge', None), ('sum', False), ('sum', True)):
            members, mapping = self.create(self.repeated_type_source(kind, monotonic),
                convert_unit='ms:s', convert_metrics=['latency'])
            cases = [('original_type', value) for value in
                     (missing, None, '', 'histogram', 'summary', 'Gauge', True, 1, [], {})]
            cases += [('is_monotonic', value) for value in
                      ((missing, True, False, 0, 1, 'false', [], {}) if kind == 'gauge' else
                       (missing, None, 0, 1, 'true', 'false', [], {}))]
            for field, value in cases:
                with self.subTest(kind=kind, monotonic=monotonic, field=field, value=repr(value)):
                    changed = copy.deepcopy(mapping)
                    if value is missing:
                        del changed['metrics'][3][field]  # Empty selected occurrence still has semantics.
                    else:
                        changed['metrics'][3][field] = value
                    self.write_changed_mapping(members, changed)
                    with self.assertRaisesRegex(ConversionError, 'bundle mapping: selected metric.*' + field):
                        bundle.inspect_bundle(self.output)
                    self.assert_rejected(field)

    def test_selected_temporality_rejects_missing_or_malformed_at_every_occurrence(self):
        missing = object()
        for kind, monotonic in (('gauge', None), ('sum', False), ('sum', True)):
            invalid = [missing, True, False, 0, 1, 3, -1, 2.0, 2.5, '2',
                       'AGGREGATION_TEMPORALITY_CUMULATIVE', '', [], [2], {}, {'value': 2}]
            invalid += [2] if kind == 'gauge' else [None]
            for strategy in (WITH, WITHOUT):
                members, mapping = self.create(self.repeated_type_source(kind, monotonic),
                    strategy=strategy, convert_unit='ms:s', convert_metrics=['latency'])
                for index in (0, 2, 3):
                    entry = mapping['metrics'][index]
                    context = (f"bundle mapping: selected metric {entry['original_name']!r} "
                               f"at {entry['path']}: aggregation_temporality")
                    for value in invalid:
                        with self.subTest(kind=kind, monotonic=monotonic, strategy=strategy,
                                          index=index, value=repr(value)):
                            changed = copy.deepcopy(mapping)
                            if value is missing:
                                del changed['metrics'][index]['aggregation_temporality']
                            else:
                                changed['metrics'][index]['aggregation_temporality'] = value
                            self.write_changed_mapping(members, changed)
                            # Integrity alone must pass: this is a mapping consistency error.
                            self.assertIs(bundle.verify_bundle(self.output)['reproduced'], False)
                            # The filter hides all three selected occurrences, including the empty one.
                            self.assert_rejected(context, metric_names=['payload'])
                            with self.assertRaises(ConversionError) as error:
                                bundle.inspect_bundle(self.output)
                            self.assertIn(context, str(error.exception))

    def test_selected_repeated_original_semantics_conflict_even_with_coherent_targets(self):
        semantics = [('gauge', None, None), ('sum', False, 2), ('sum', True, 2)]
        for kind, monotonic, temporality in semantics:
            for strategy in (WITH, WITHOUT):
                members, mapping = self.create(self.repeated_type_source(kind, monotonic),
                    strategy=strategy, convert_unit='ms:s', convert_metrics=['latency'])
                for other in semantics:
                    if other == (kind, monotonic, temporality):
                        continue
                    for index in (0, 2, 3):
                        with self.subTest(kind=kind, monotonic=monotonic, strategy=strategy,
                                          other=other, index=index):
                            changed = copy.deepcopy(mapping)
                            counter = other[1] is True
                            family = 'latency_seconds' if strategy == WITH else 'latency'
                            target = family + ('_total' if counter and strategy == WITH else '')
                            changed['metrics'][index].update(original_type=other[0],
                                is_monotonic=other[1], aggregation_temporality=other[2],
                                target_type='counter' if counter else 'gauge', target_family=target,
                                parser_family=family, parser_sample=family + ('_total' if counter else ''))
                            self.write_changed_mapping(members, changed)
                            self.assertIs(bundle.verify_bundle(self.output)['reproduced'], False)
                            # First, repeated and empty occurrences all participate, even hidden ones.
                            self.assert_rejected('conflicting original descriptor semantics', metric_names=['payload'])
                            with self.assertRaises(ConversionError) as error:
                                bundle.inspect_bundle(self.output)
                            message = str(error.exception)
                            self.assertIn("bundle mapping: selected metric 'latency'", message)
                            self.assertIn(mapping['metrics'][0]['path'], message)
                            self.assertIn(mapping['metrics'][3 if index == 3 else 2]['path'], message)
                            for field in ('original_type', 'is_monotonic', 'aggregation_temporality'):
                                self.assertIn(field, message)

    @staticmethod
    def series_source(kind='sum', monotonic=True):
        def metric(site=None, empty=False):
            result = (gauge('wait.b_total', 'ms') if kind == 'gauge' else
                      sum_metric('wait.b_total', 'ms', monotonic=monotonic))
            result[kind]['dataPoints'] = ([] if empty else [{'asInt': '42',
                'attributes': [attr('site', site), attr('region', 'east')]}])
            return result
        first = metric('a')
        first[kind]['dataPoints'] += metric('b')[kind]['dataPoints']
        return json.dumps({'resourceMetrics': [
            {'scopeMetrics': [{'metrics': [gauge('wait.a', 'ms'), first,
                                           metric(empty=True), metric('c')]},
                              {'metrics': [metric('d')]}]},
            {'scopeMetrics': [{'metrics': [metric('e'), metric(empty=True)]}]}]}).encode()

    def test_selected_final_series_rejects_duplicates_within_and_across_occurrences(self):
        for kind, monotonic in [('gauge', None), ('sum', False), ('sum', True)]:
            for strategy in (WITH, WITHOUT):
                members, mapping = self.create(self.series_source(kind, monotonic), strategy=strategy,
                    convert_unit='ms:s', convert_metrics=['wait.b_total'])
                first = mapping['metrics'][1]['data_points'][0]
                # Same descriptor, same scope, different scope, different resource.
                for index, point_index in [(1, 1), (3, 0), (4, 0), (5, 0)]:
                    with self.subTest(kind=kind, monotonic=monotonic, strategy=strategy, index=index):
                        changed = copy.deepcopy(mapping)
                        current = changed['metrics'][index]['data_points'][point_index]
                        current['labels'] = list(reversed(copy.deepcopy(first['labels'])))
                        current['original_value'] = '123456'  # Keep the conversion relationship valid.
                        current['value'] = '123.456'  # Values do not distinguish final series.
                        self.write_changed_mapping(members, changed)
                        self.assertIs(bundle.verify_bundle(self.output)['reproduced'], False)
                        for names in (None, ['wait.a']):
                            self.assert_rejected('duplicate canonical final-series identity', metric_names=names)
                        with self.assertRaises(ConversionError) as caught:
                            bundle.inspect_bundle(self.output)
                        for text in ('bundle mapping', repr('wait.b_total'),
                                     repr(mapping['metrics'][1]['parser_sample']),
                                     "('region', 'east')", "('site', 'a')", first['path'], current['path']):
                            self.assertIn(text, str(caught.exception))

    def test_selected_final_series_equates_empty_absent_and_scalar_label_values(self):
        for strategy in (WITH, WITHOUT):
            for first_attrs, second_attrs in [([], [attr('site', 'b')]),
                    ([attr('site', '')], [attr('site', 'b')]),
                    ([attr('site', 1, 'intValue')], [attr('site', '2')])]:
                with self.subTest(strategy=strategy, attrs=first_attrs):
                    source = document(gauge('wait.b_total', 'ms', attrs=first_attrs),
                                      gauge('wait.b_total', 'ms', attrs=second_attrs), gauge('wait.a'))
                    members, mapping = self.create(json.dumps(source).encode(), strategy=strategy,
                        convert_unit='ms:s', convert_metrics=['wait.b_total'])
                    changed = copy.deepcopy(mapping)
                    # Empty and absent labels canonicalize together; int/string provenance
                    # cannot distinguish the same mapped value string.
                    changed['metrics'][1]['data_points'][0]['labels'][0]['value'] = (
                        '1' if first_attrs and first_attrs[0]['value'].get('intValue') == 1 else '')
                    self.write_changed_mapping(members, changed)
                    self.assert_rejected('duplicate canonical final-series identity', metric_names=['wait.a'])

    def test_selected_final_series_preserves_distinct_labels_empty_descriptors_and_replay(self):
        for strategy in (WITH, WITHOUT):
            source = self.series_source()
            self.create(source, strategy=strategy, convert_unit='ms:s', convert_metrics=['wait.b_total'])
            before = self.output.read_bytes()
            result = bundle.inspect_bundle(self.output)
            self.assertEqual((result['sample_count'], result['metric_count']), (6, 7))
            self.assertEqual([m['sample_count'] for m in result['metrics']], [1, 2, 0, 1, 1, 1, 0])
            self.assertEqual(bundle.inspect_bundle(self.output, source=source), {**result, 'reproduced': True})
            first = self.run_cli()
            self.assertEqual((first.returncode, first.stderr), (0, ''))
            self.assertEqual(json.loads(first.stdout), result)
            self.assertEqual(first.stdout, self.run_cli().stdout)
            self.assertEqual(self.output.read_bytes(), before)

    def test_selected_final_series_requires_usable_point_and_label_identity_fields(self):
        missing = object()
        for strategy in (WITH, WITHOUT):
            members, mapping = self.create(self.series_source(), strategy=strategy,
                convert_unit='ms:s', convert_metrics=['wait.b_total'])
            cases = [('point', value) for value in (None, [], 'point', 1)]
            cases += [('path', value) for value in (missing, None, '', 1)]
            cases += [('labels', value) for value in (missing, None, {}, 'labels')]
            cases += [('label', value) for value in (None, [], 'label')]
            cases += [('target_label', value) for value in (missing, None, '', 1)]
            cases += [('value', value) for value in (missing, None, 1, True, [])]
            cases += [('duplicate_target', None)]
            for field, value in cases:
                with self.subTest(strategy=strategy, field=field, value=repr(value)):
                    changed = copy.deepcopy(mapping)
                    point = changed['metrics'][5]['data_points'][0]
                    if field == 'point':
                        changed['metrics'][5]['data_points'][0] = value
                    elif field == 'label':
                        point['labels'][0] = value
                    elif field == 'duplicate_target':
                        point['labels'].append(copy.deepcopy(point['labels'][0]))
                    else:
                        target = point['labels'][0] if field in ('target_label', 'value') else point
                        if value is missing:
                            del target[field]
                        else:
                            target[field] = value
                    self.write_changed_mapping(members, changed)
                    self.assert_rejected("bundle mapping: selected metric 'wait.b_total'", metric_names=['wait.a'])

    def test_final_series_check_leaves_legacy_and_unselected_points_unchanged(self):
        for strategy in (WITH, WITHOUT):
            for selection in (None, ['wait.a']):
                members, mapping = self.create(self.series_source(), strategy=strategy,
                    convert_unit='ms:s', convert_metrics=selection)
                for malformed in (False, True):
                    with self.subTest(strategy=strategy, selection=selection, malformed=malformed):
                        changed = copy.deepcopy(mapping)
                        changed['metrics'][5]['data_points'][0]['labels'] = copy.deepcopy(
                            changed['metrics'][1]['data_points'][0]['labels'])
                        if malformed:
                            changed['metrics'][3]['data_points'][0] = None
                        self.write_changed_mapping(members, changed)
                        first = self.run_cli()
                        self.assertEqual((first.returncode, first.stderr), (0, ''))
                        self.assertEqual(first.stdout, self.run_cli().stdout)

    def test_selected_label_owners_conflict_across_points_before_filtering(self):
        for strategy in (WITH, WITHOUT):
            for kind, monotonic in [('gauge', None), ('sum', False), ('sum', True)]:
                members, mapping = self.create(self.series_source(kind, monotonic), strategy=strategy,
                    convert_unit='ms:s', convert_metrics=['wait.b_total'])
                for index, point_index in [(1, 1), (3, 0), (4, 0), (5, 0)]:
                    for empty in (False, True):
                        with self.subTest(strategy=strategy, kind=kind, monotonic=monotonic,
                                          index=index, empty=empty):
                            changed = copy.deepcopy(mapping)
                            first = changed['metrics'][1]['data_points'][0]
                            current = changed['metrics'][index]['data_points'][point_index]
                            # Distinct series remain distinct, including when both values are empty.
                            for point, key in [(first, 'site.a'), (current, 'site-a')]:
                                label = next(label for label in point['labels'] if label['key'] == 'site')
                                label.update(key=key, target_label='site_a')
                                if empty:
                                    label['value'] = ''
                                    point['labels'].append({'key': 'id', 'target_label': 'id',
                                        'value': point['path'], 'path': point['path'] + '.attributes[2]'})
                            self.write_changed_mapping(members, changed)
                            self.assertIs(bundle.verify_bundle(self.output)['reproduced'], False)
                            for names in (None, ['wait.a']):
                                self.assert_rejected('conflicting target_label', metric_names=names)
                            with self.assertRaises(ConversionError) as caught:
                                bundle.inspect_bundle(self.output)
                            for text in ("bundle mapping: selected metric 'wait.b_total'", 'site_a',
                                         repr('site.a'), repr('site-a'), first['path'], current['path'],
                                         first['labels'][1]['path'], current['labels'][1]['path']):
                                self.assertIn(text, str(caught.exception))

    def test_selected_label_owners_require_literal_keys_and_label_paths(self):
        missing = object()
        for strategy in (WITH, WITHOUT):
            members, mapping = self.create(self.series_source(), strategy=strategy,
                convert_unit='ms:s', convert_metrics=['wait.b_total'])
            for field in ('key', 'path'):
                for value in (missing, None, '', True, 42, [], {}):
                    with self.subTest(strategy=strategy, field=field, value=repr(value)):
                        changed = copy.deepcopy(mapping)
                        label = changed['metrics'][5]['data_points'][0]['labels'][0]
                        if value is missing:
                            del label[field]
                        else:
                            label[field] = value
                        self.write_changed_mapping(members, changed)
                        self.assert_rejected('label ' + field, metric_names=['wait.a'])

    def test_selected_label_owners_preserve_same_keys_distinct_labels_families_and_replay(self):
        source = json.loads(self.series_source())
        # The same mapped label may have a different owner in a different family.
        source['resourceMetrics'][0]['scopeMetrics'][0]['metrics'][0]['gauge']['dataPoints'][0][
            'attributes'] = [attr('site-a', 'elsewhere')]
        for resource in source['resourceMetrics']:
            for scope in resource['scopeMetrics']:
                for metric in scope['metrics']:
                    if metric['name'] == 'wait.b_total':
                        for point in metric['sum']['dataPoints']:
                            point['attributes'][0]['key'] = 'site.a'
        source = json.dumps(source).encode()
        for strategy in (WITH, WITHOUT):
            self.create(source, strategy=strategy, convert_unit='ms:s',
                        convert_metrics=['wait.a', 'wait.b_total'])
            result = bundle.inspect_bundle(self.output)
            self.assertEqual([m['sample_count'] for m in result['metrics']], [1, 2, 0, 1, 1, 1, 0])
            self.assertEqual(bundle.inspect_bundle(self.output, source=source), {**result, 'reproduced': True})
            first = self.run_cli(metric_names=['wait.a'])
            self.assertEqual((first.returncode, first.stderr), (0, ''))
            self.assertEqual(first.stdout, self.run_cli(metric_names=['wait.a']).stdout)
        # Ownership is literal, including whitespace and Unicode, with no normalization of keys.
        members, mapping = self.create(self.series_source(), convert_unit='ms:s',
            convert_metrics=['wait.b_total'])
        for other in (' site ', 'Site', 'site.µ', 'site.e\u0301'):
            changed = copy.deepcopy(mapping)
            changed['metrics'][5]['data_points'][0]['labels'][1]['key'] = other
            self.write_changed_mapping(members, changed)
            self.assert_rejected('conflicting target_label', metric_names=['wait.a'])

    def test_label_owners_leave_legacy_and_unselected_validation_unchanged(self):
        for strategy in (WITH, WITHOUT):
            for selection in (None, ['wait.a']):
                members, mapping = self.create(self.series_source(), strategy=strategy,
                    convert_unit='ms:s', convert_metrics=selection)
                before = self.run_cli()
                changed = copy.deepcopy(mapping)
                changed['metrics'][3]['data_points'][0]['labels'][1]['key'] = 'site-a'
                for label in changed['metrics'][5]['data_points'][0]['labels']:
                    label.pop('key')
                    label.pop('path')
                self.write_changed_mapping(members, changed)
                after = self.run_cli()
                self.assertEqual((before.returncode, after.returncode, after.stderr), (0, 0, ''))
                self.assertEqual(before.stdout, after.stdout)

    def label_source(self):
        source = json.loads(self.series_source())
        for index, resource in enumerate(source['resourceMetrics']):
            resource['resource'] = {'attributes': [attr('rack.zone', str(index))]}
        return json.dumps(source).encode()

    def test_selected_converted_values_reject_coherent_rewrites_before_filtering(self):
        for strategy in (WITH, WITHOUT):
            members, mapping = self.create(self.series_source(), strategy=strategy,
                convert_unit='ms:s', convert_metrics=['wait.b_total'])
            changed = copy.deepcopy(mapping)
            for metric in changed['metrics']:
                if metric['original_name'] == 'wait.b_total':
                    for point in metric['data_points']:
                        point.update(original_value='1000', value='7.000')
            coherent = {**members, 'exposition.prom': members['exposition.prom'].replace(b' 0.042', b' 7.000')}
            manifest = json.loads(coherent['manifest.json'])
            manifest['artifacts']['exposition.prom'] = bundle.digest(coherent['exposition.prom'])
            coherent['manifest.json'] = bundle.json_bytes(manifest)
            self.write_changed_mapping(coherent, changed)
            with self.subTest(strategy=strategy):
                self.assertFalse(bundle.verify_bundle(self.output)['reproduced'])
                self.assert_rejected('expected exact ms:s value', metric_names=['wait.a'])
                with self.assertRaises(ConversionError) as caught:
                    bundle.inspect_bundle(self.output)
                for text in ("selected metric 'wait.b_total'", changed['metrics'][1]['data_points'][0]['path'],
                             "original_value '1000'", "value '7.000'", "'1.000'"):
                    self.assertIn(text, str(caught.exception))

    def test_selected_converted_values_check_every_point_and_exact_text(self):
        for strategy in (WITH, WITHOUT):
            members, mapping = self.create(self.series_source(), strategy=strategy,
                convert_unit='ms:s', convert_metrics=['wait.b_total'])
            for metric_index, point_index in [(1, 0), (1, 1), (3, 0), (4, 0), (5, 0)]:
                for stored in ('7.000', '0.0420', '4.2e-2'):
                    with self.subTest(strategy=strategy, metric=metric_index, point=point_index, value=stored):
                        changed = copy.deepcopy(mapping)
                        point = changed['metrics'][metric_index]['data_points'][point_index]
                        point['value'] = stored
                        self.write_changed_mapping(members, changed)
                        self.assert_rejected('expected exact ms:s value', metric_names=['wait.a'])
                        with self.assertRaises(ConversionError) as caught:
                            bundle.inspect_bundle(self.output)
                        for text in (point['path'], "original_value '42'", repr(stored), "'0.042'"):
                            self.assertIn(text, str(caught.exception))

    def test_selected_converted_values_require_usable_strings_and_nonfinite_opt_in(self):
        invalid = [None, False, 1, [], {}, '', ' ', 'abc', ' 42', '4_2',
                   'Infinity', '-Infinity', 'Inf', 'sNaN', 'NaN', '+Inf', '-Inf', '1e999', '1e-999', '1e-323']
        for strategy in (WITH, WITHOUT):
            members, mapping = self.create(self.series_source(), strategy=strategy,
                convert_unit='ms:s', convert_metrics=['wait.b_total'])
            for field in ('original_value', 'value'):
                for missing, value in [(True, None)] + [(False, v) for v in invalid]:
                    with self.subTest(strategy=strategy, field=field, missing=missing, value=value):
                        changed = copy.deepcopy(mapping)
                        point = changed['metrics'][5]['data_points'][0]
                        if missing:
                            del point[field]
                        else:
                            point[field] = value
                        self.write_changed_mapping(members, changed)
                        with self.assertRaises(ConversionError) as caught:
                            bundle.inspect_bundle(self.output, metric_names=['wait.a'])
                        for text in ("selected metric 'wait.b_total'", point['path'], field):
                            self.assertIn(text, str(caught.exception))
            self.assert_rejected('original_value', metric_names=['wait.a'])

    def test_selected_converted_values_preserve_exact_numeric_text_empty_descriptors_and_replay(self):
        numeric = ["-9223372036854775808", "9223372036854775807", '0', '0.0', '-0.0',
                   '0.100000000000000000001', '1.2300e+20', '1e300', '1e-320',
                   '"NaN"', '"Infinity"', '"-Infinity"']
        points = ','.join('{"%s":%s,"attributes":[{"key":"site","value":{"intValue":%d}}]}' %
            ('asInt' if i < 2 else 'asDouble', ('"' + value + '"') if i < 2 else value, i)
            for i, value in enumerate(numeric))
        source = ('{"resourceMetrics":[{"scopeMetrics":[{"metrics":[{"name":"wait.a","unit":"ms",'
                  '"gauge":{"dataPoints":[' + points + ']}}, {"name":"wait.a","unit":"ms",'
                  '"gauge":{"dataPoints":[]}}]}]}]}').encode()
        for strategy in (WITH, WITHOUT):
            members, mapping = self.create(source, strategy=strategy, allow_nonfinite=True,
                convert_unit='ms:s', convert_metrics=['wait.a'])
            summary = bundle.inspect_bundle(self.output)
            self.assertEqual(summary['sample_count'], len(numeric))
            self.assertEqual(summary['metrics'][1]['sample_count'], 0)
            self.assertEqual(bundle.inspect_bundle(self.output, source=source), {**summary, 'reproduced': True})
            self.assertEqual(bundle.inspect_bundle(self.output), summary)
            # Opt-in does not permit changing one nonfinite token to another.
            changed = copy.deepcopy(mapping)
            changed['metrics'][0]['data_points'][-1]['value'] = '+Inf'
            self.write_changed_mapping(members, changed)
            self.assert_rejected('expected exact ms:s value')

    def test_converted_value_checks_leave_legacy_and_unselected_points_unchanged(self):
        for strategy in (WITH, WITHOUT):
            for selection in (None, ['wait.a']):
                members, mapping = self.create(self.series_source(), strategy=strategy,
                    convert_unit='ms:s', convert_metrics=selection)
                summary = bundle.inspect_bundle(self.output)
                changed = copy.deepcopy(mapping)
                for metric in changed['metrics']:
                    if metric['original_name'] == 'wait.b_total':
                        for point in metric['data_points']:
                            point.update(original_value=None, value='not a sample')
                self.write_changed_mapping(members, changed)
                self.assertEqual(bundle.inspect_bundle(self.output), summary)

    def test_selected_label_normalization_rejects_coherent_wrong_targets(self):
        for strategy in (WITH, WITHOUT):
            members, mapping = self.create(self.label_source(), strategy=strategy,
                promote_resource=['rack.zone'], convert_unit='ms:s', convert_metrics=['wait.b_total'])
            changed = copy.deepcopy(mapping)
            for metric in changed['metrics']:
                if metric['original_name'] == 'wait.b_total':
                    for point in metric['data_points']:
                        label = next(label for label in point['labels'] if label['key'] == 'site')
                        label.update(key='site.a', target_label='site_wrong', normalizations=[{
                            'operation': 'label_underscore_escape_or_digit_prefix',
                            'before': 'site.a', 'after': 'site_wrong'}])
            coherent = {**members, 'exposition.prom': members['exposition.prom'].replace(b'site=', b'site_wrong=')}
            manifest = json.loads(coherent['manifest.json'])
            manifest['artifacts']['exposition.prom'] = bundle.digest(coherent['exposition.prom'])
            coherent['manifest.json'] = bundle.json_bytes(manifest)
            self.write_changed_mapping(coherent, changed)
            with self.subTest(strategy=strategy):
                self.assertIs(bundle.verify_bundle(self.output)['reproduced'], False)
                self.assert_rejected('expected normalized label', metric_names=['wait.a'])
                with self.assertRaises(ConversionError) as caught:
                    bundle.inspect_bundle(self.output)
                for text in ("selected metric 'wait.b_total'", repr('site.a'), repr('site_wrong'), repr('site_a')):
                    self.assertIn(text, str(caught.exception))

    def test_selected_label_normalization_checks_every_label_before_filtering(self):
        for strategy in (WITH, WITHOUT):
            members, mapping = self.create(self.label_source(), strategy=strategy,
                promote_resource=['rack.zone'], convert_unit='ms:s', convert_metrics=['wait.b_total'])
            for index, point_index in [(1, 0), (1, 1), (3, 0), (4, 0), (5, 0)]:
                for source, key, expected in [('datapoint', 'site', 'site'), ('resource', 'rack.zone', 'rack_zone')]:
                    for empty in (False, True):
                        with self.subTest(strategy=strategy, index=index, point=point_index, source=source, empty=empty):
                            changed = copy.deepcopy(mapping)
                            point = changed['metrics'][index]['data_points'][point_index]
                            label = next(label for label in point['labels'] if label['key'] == key)
                            label['target_label'] = 'wrong_label'
                            if empty:
                                label['value'] = ''
                            self.write_changed_mapping(members, changed)
                            self.assert_rejected('expected normalized label', metric_names=['wait.a'])
                            with self.assertRaises(ConversionError) as caught:
                                bundle.inspect_bundle(self.output)
                            for text in ("selected metric 'wait.b_total'", point['path'], label['path'],
                                         repr(key), repr('wrong_label'), repr(expected)):
                                self.assertIn(text, str(caught.exception))
            for source, key in [('datapoint', 'site'), ('resource', 'rack.zone')]:
                for raw in ('__site', '..site', '  ', '__name__'):
                    changed = copy.deepcopy(mapping)
                    # A consistent raw owner with an impossible name cannot evade feasibility.
                    for metric in changed['metrics']:
                        if metric['original_name'] == 'wait.b_total':
                            for point in metric['data_points']:
                                label = next(label for label in point['labels'] if label['key'] == key)
                                label.update(key=raw, target_label='wrong_label', value='')
                    self.write_changed_mapping(members, changed)
                    with self.subTest(strategy=strategy, source=source, raw=raw):
                        self.assert_rejected('reserved Prometheus label', metric_names=['wait.a'])
                        with self.assertRaises(ConversionError) as caught:
                            bundle.inspect_bundle(self.output)
                        point = changed['metrics'][1]['data_points'][0]
                        label = next(label for label in point['labels'] if label['key'] == raw)
                        for text in ("selected metric 'wait.b_total'", point['path'], label['path'], repr(raw),
                                     repr('wrong_label')):
                            self.assertIn(text, str(caught.exception))

    def test_selected_label_normalization_preserves_naming_rules_and_replay(self):
        source = json.loads(self.label_source())
        expected = {'site.a': 'site_a', 'rack.zone': 'rack_zone', '9.zone': 'key_9_zone',
                    'rack__zone': 'rack__zone', '_site': '_site', 'µ': '_', 'x..y': 'x__y'}
        for resource in source['resourceMetrics']:
            for scope in resource['scopeMetrics']:
                for metric in scope['metrics']:
                    for point in metric.get('sum', {}).get('dataPoints', []):
                        point['attributes'][0]['key'] = 'site.a'
                        point['attributes'] += [attr(key, '') for key in expected if key not in ('site.a', 'rack.zone')]
        source = json.dumps(source).encode()
        for strategy in (WITH, WITHOUT):
            _, mapping = self.create(source, strategy=strategy, promote_resource=['rack.zone'],
                convert_unit='ms:s', convert_metrics=['wait.b_total'])
            for metric in mapping['metrics']:
                if metric['original_name'] == 'wait.b_total':
                    for point in metric['data_points']:
                        self.assertEqual({label['key']: label['target_label'] for label in point['labels']},
                                         {**expected, 'region': 'region'})
            result = bundle.inspect_bundle(self.output)
            self.assertEqual(bundle.inspect_bundle(self.output, source=source), {**result, 'reproduced': True})
            self.assertEqual([m['sample_count'] for m in result['metrics']], [1, 2, 0, 1, 1, 1, 0])
            first = self.run_cli(metric_names=['wait.a'])
            self.assertEqual((first.returncode, first.stderr), (0, ''))
            self.assertEqual(first.stdout, self.run_cli(metric_names=['wait.a']).stdout)

    def test_label_normalization_leaves_legacy_and_unselected_validation_unchanged(self):
        for strategy in (WITH, WITHOUT):
            for selection in (None, ['wait.a']):
                members, mapping = self.create(self.series_source(), strategy=strategy,
                    convert_unit='ms:s', convert_metrics=selection)
                before = self.run_cli()
                for raw in ('site.a', '__site'):
                    changed = copy.deepcopy(mapping)
                    for metric in changed['metrics']:
                        if metric['original_name'] == 'wait.b_total':
                            for point in metric['data_points']:
                                point['labels'][1].update(key=raw, target_label='wrong_label')
                    self.write_changed_mapping(members, changed)
                    after = self.run_cli()
                    self.assertEqual((before.returncode, after.returncode, after.stderr), (0, 0, ''))
                    self.assertEqual(before.stdout, after.stdout)

    def test_selected_parser_family_rejects_distinct_raw_owners_before_filtering(self):
        for counter_first in (False, True):
            first, alias = ('wait-a_total', 'wait.a') if counter_first else ('wait.a', 'wait-a_total')
            source = self.repeated_type_source('sum' if counter_first else 'gauge',
                                               True if counter_first else None).replace(b'latency', first.encode())
            for strategy in (WITH, WITHOUT):
                members, mapping = self.create(source, strategy=strategy,
                    convert_unit='ms:s', convert_metrics=[first])
                family = 'wait_a_seconds' if strategy == WITH else 'wait_a'
                for first_empty in (False, True):
                    for indexes in ((2,), (3,), (2, 3)):
                        with self.subTest(counter_first=counter_first, strategy=strategy,
                                          first_empty=first_empty, indexes=indexes):
                            changed = copy.deepcopy(mapping)
                            names = sorted([first, alias])
                            changed['unit_conversion_metrics'] = names
                            if first_empty:
                                changed['metrics'][0]['data_points'] = []
                                changed['sample_count'] -= 1
                            for index in indexes:
                                changed['metrics'][index].update(original_name=alias,
                                    original_type='gauge' if counter_first else 'sum',
                                    is_monotonic=None if counter_first else True,
                                    aggregation_temporality=None if counter_first else 2,
                                    target_type='gauge' if counter_first else 'counter',
                                    target_family=family if counter_first else family + '_total',
                                    parser_family=family,
                                    parser_sample=family if counter_first else family + '_total')
                            self.assertNotEqual(changed['metrics'][0]['target_family'],
                                                changed['metrics'][indexes[0]]['target_family'])
                            raw = bundle.json_bytes(changed)
                            manifest = json.loads(members['manifest.json'])
                            manifest['configuration']['convert_metrics'] = names
                            manifest['artifacts']['mapping.json'] = bundle.digest(raw)
                            bundle.atomic_write_bundle(self.output, {**members, 'mapping.json': raw,
                                'manifest.json': bundle.json_bytes(manifest)})
                            self.assertIs(bundle.verify_bundle(self.output)['reproduced'], False)
                            for filter_names in (None, [first], [alias], ['payload']):
                                self.assert_rejected('conflicting parser_family', metric_names=filter_names)
                            with self.assertRaises(ConversionError) as caught:
                                bundle.inspect_bundle(self.output)
                            for text in ('bundle mapping', 'conflicting parser_family', repr(family),
                                         repr(first), repr(alias), mapping['metrics'][0]['path'],
                                         mapping['metrics'][indexes[0]]['path']):
                                self.assertIn(text, str(caught.exception))

    def test_selected_parser_family_preserves_repeats_distinct_names_and_exact_replay(self):
        for strategy in (WITH, WITHOUT):
            for kind, monotonic in (('gauge', None), ('sum', True)):
                source = json.loads(self.repeated_type_source(kind, monotonic))
                # An empty first owner and later nonempty/empty repeats remain valid.
                source['resourceMetrics'][0]['scopeMetrics'][0]['metrics'][0][kind]['dataPoints'] = []
                source['resourceMetrics'][1]['scopeMetrics'][1]['metrics'].append(gauge('wait.b', 'ms'))
                raw = json.dumps(source).encode()
                self.create(raw, strategy=strategy, convert_unit='ms:s', convert_metrics=['latency', 'wait.b'])
                result = bundle.inspect_bundle(self.output)
                self.assertEqual([m['sample_count'] for m in result['metrics']], [0, 1, 1, 0, 1])
                self.assertNotEqual(result['metrics'][0]['parser_family'], result['metrics'][-1]['parser_family'])
                self.assertEqual(bundle.inspect_bundle(self.output, source=raw), {**result, 'reproduced': True})
                first = self.run_cli()
                self.assertEqual((first.returncode, first.stderr), (0, ''))
                self.assertEqual(json.loads(first.stdout), result)
                self.assertEqual(first.stdout, self.run_cli().stdout)

    def test_parser_family_ownership_leaves_legacy_and_unselected_validation_unchanged(self):
        source = json.dumps(document(gauge('wait.a', 'ms'), sum_metric('wait.b_total', 'ms'),
                                     gauge('other', 'ms'))).encode()
        for strategy in (WITH, WITHOUT):
            for selection in (None, ['other'], ['wait.a'], ['wait.b_total']):
                with self.subTest(strategy=strategy, selection=selection):
                    members, mapping = self.create(source, strategy=strategy,
                        convert_unit='ms:s', convert_metrics=selection)
                    # Forge only unselected naming fields: there is at most one selected owner.
                    if selection == ['wait.b_total']:
                        mapping['metrics'][0]['parser_family'] = mapping['metrics'][1]['parser_family']
                        mapping['metrics'][0]['parser_sample'] = mapping['metrics'][1]['parser_family']
                    else:
                        mapping['metrics'][1]['parser_family'] = mapping['metrics'][0]['parser_family']
                        mapping['metrics'][1]['parser_sample'] = mapping['metrics'][0]['parser_family'] + '_total'
                    self.assertNotEqual(mapping['metrics'][0]['target_family'], mapping['metrics'][1]['target_family'])
                    raw = bundle.json_bytes(mapping)
                    manifest = json.loads(members['manifest.json'])
                    manifest['artifacts']['mapping.json'] = bundle.digest(raw)
                    bundle.atomic_write_bundle(self.output, {**members, 'mapping.json': raw,
                        'manifest.json': bundle.json_bytes(manifest)})
                    first = self.run_cli()
                    self.assertEqual((first.returncode, first.stderr), (0, ''))
                    self.assertEqual(first.stdout, self.run_cli().stdout)

    def test_selected_target_family_rejects_distinct_raw_owners_before_filtering(self):
        for kind, monotonic in [('gauge', None), ('sum', False), ('sum', True)]:
            source = self.repeated_type_source(kind, monotonic).replace(b'latency', b'wait.a')
            for strategy in (WITH, WITHOUT):
                members, mapping = self.create(source, strategy=strategy,
                    convert_unit='ms:s', convert_metrics=['wait.a'])
                for alias in ('wait-a', ' wait.a '):
                    for indexes in ((2,), (3,), (2, 3)):
                        with self.subTest(kind=kind, monotonic=monotonic, strategy=strategy,
                                          alias=alias, indexes=indexes):
                            changed = copy.deepcopy(mapping)
                            names = sorted(['wait.a', alias])
                            changed['unit_conversion_metrics'] = names
                            for index in indexes:
                                changed['metrics'][index]['original_name'] = alias
                            raw = bundle.json_bytes(changed)
                            manifest = json.loads(members['manifest.json'])
                            manifest['configuration']['convert_metrics'] = names
                            manifest['artifacts']['mapping.json'] = bundle.digest(raw)
                            bundle.atomic_write_bundle(self.output, {**members, 'mapping.json': raw,
                                'manifest.json': bundle.json_bytes(manifest)})
                            self.assertIs(bundle.verify_bundle(self.output)['reproduced'], False)
                            for filter_names in (None, ['wait.a'], [alias], ['payload']):
                                self.assert_rejected('conflicting target_family', metric_names=filter_names)
                            with self.assertRaises(ConversionError) as caught:
                                bundle.inspect_bundle(self.output)
                            message = str(caught.exception)
                            for text in ('bundle mapping', 'conflicting target_family',
                                         repr(mapping['metrics'][0]['target_family']), repr('wait.a'),
                                         repr(alias), mapping['metrics'][0]['path'],
                                         mapping['metrics'][indexes[0]]['path']):
                                self.assertIn(text, message)

    def test_selected_target_family_preserves_repeats_distinct_names_and_exact_replay(self):
        for strategy in (WITH, WITHOUT):
            source = json.loads(self.repeated_type_source('gauge').replace(b'latency', b'wait.a'))
            source['resourceMetrics'][1]['scopeMetrics'][1]['metrics'].append(gauge('wait.b', 'ms'))
            raw = json.dumps(source).encode()
            self.create(raw, strategy=strategy, convert_unit='ms:s', convert_metrics=['wait.a', 'wait.b'])
            result = bundle.inspect_bundle(self.output)
            self.assertEqual([m['original_name'] for m in result['metrics']],
                             ['wait.a', 'payload', 'wait.a', 'wait.a', 'wait.b'])
            self.assertEqual([m['sample_count'] for m in result['metrics']], [1, 1, 1, 0, 1])
            self.assertEqual(bundle.inspect_bundle(self.output, source=raw), {**result, 'reproduced': True})
            first = self.run_cli()
            self.assertEqual((first.returncode, first.stderr), (0, ''))
            self.assertEqual(json.loads(first.stdout), result)
            self.assertEqual(first.stdout, self.run_cli().stdout)

    def test_target_family_ownership_leaves_legacy_and_unselected_validation_unchanged(self):
        source = json.dumps(document(gauge('wait.a', 'ms'), gauge('wait.b', 'ms'),
                                     gauge('other', 'ms'))).encode()
        for strategy in (WITH, WITHOUT):
            for selection in (None, ['other'], ['wait.a']):
                with self.subTest(strategy=strategy, selection=selection):
                    members, mapping = self.create(source, strategy=strategy,
                        convert_unit='ms:s', convert_metrics=selection)
                    # Both unselected owners (or just one selected owner) retain prior validation.
                    mapping['metrics'][1].update(original_name='wait-a',
                        **{field: mapping['metrics'][0][field]
                           for field in ('target_family', 'parser_family', 'parser_sample')})
                    raw = bundle.json_bytes(mapping)
                    manifest = json.loads(members['manifest.json'])
                    manifest['artifacts']['mapping.json'] = bundle.digest(raw)
                    bundle.atomic_write_bundle(self.output, {**members, 'mapping.json': raw,
                        'manifest.json': bundle.json_bytes(manifest)})
                    first = self.run_cli()
                    self.assertEqual((first.returncode, first.stderr), (0, ''))
                    self.assertEqual(first.stdout, self.run_cli().stdout)

    def test_selected_original_descriptions_conflict_at_every_occurrence(self):
        pairs = [('', 'Contradictory help text'), ('Help', ''),
                 ('café', 'cafe\u0301'), ('line\nhelp', r'line\nhelp')]
        for kind, monotonic in (('gauge', None), ('sum', False), ('sum', True)):
            for strategy in (WITH, WITHOUT):
                source = json.loads(self.repeated_type_source(kind, monotonic))
                for first, other in pairs:
                    for resource in source['resourceMetrics']:
                        for scope in resource['scopeMetrics']:
                            for metric in scope['metrics']:
                                metric['description'] = first
                    members, mapping = self.create(json.dumps(source).encode(), strategy=strategy,
                        convert_unit='ms:s', convert_metrics=['latency'])
                    for index in (0, 2, 3):
                        with self.subTest(kind=kind, monotonic=monotonic, strategy=strategy,
                                          first=first, other=other, index=index):
                            changed = copy.deepcopy(mapping)
                            changed['metrics'][index]['original_description'] = other
                            self.write_changed_mapping(members, changed)
                            self.assertIs(bundle.verify_bundle(self.output)['reproduced'], False)
                            self.assert_rejected('conflicting original_description', metric_names=['payload'])
                            with self.assertRaises(ConversionError) as error:
                                bundle.inspect_bundle(self.output)
                            message = str(error.exception)
                            self.assertIn("bundle mapping: selected metric 'latency'", message)
                            self.assertIn(mapping['metrics'][0]['path'], message)
                            self.assertIn(mapping['metrics'][3 if index == 3 else 2]['path'], message)

    def test_selected_original_descriptions_require_strings_at_every_occurrence(self):
        missing = object()
        for strategy in (WITH, WITHOUT):
            members, mapping = self.create(self.repeated_type_source('gauge'), strategy=strategy,
                convert_unit='ms:s', convert_metrics=['latency'])
            for index in (0, 2, 3):
                context = ("bundle mapping: selected metric 'latency' at "
                           + mapping['metrics'][index]['path'] + ': original_description')
                for value in (missing, None, True, False, 0, 1.5, [], {}, ['help']):
                    with self.subTest(strategy=strategy, index=index, value=repr(value)):
                        changed = copy.deepcopy(mapping)
                        if value is missing:
                            del changed['metrics'][index]['original_description']
                        else:
                            changed['metrics'][index]['original_description'] = value
                        self.write_changed_mapping(members, changed)
                        self.assertIs(bundle.verify_bundle(self.output)['reproduced'], False)
                        self.assert_rejected(context, metric_names=['payload'])
                        with self.assertRaises(ConversionError) as error:
                            bundle.inspect_bundle(self.output)
                        self.assertIn(context, str(error.exception))
            # Existing whole-mapping UTF-8 validation also rejects escaped lone surrogates.
            changed = copy.deepcopy(mapping)
            changed['metrics'][3]['original_description'] = '\ud800'
            raw = json.dumps(changed).encode()
            manifest = json.loads(members['manifest.json'])
            manifest['artifacts']['mapping.json'] = bundle.digest(raw)
            bundle.atomic_write_bundle(self.output, {**members, 'mapping.json': raw,
                'manifest.json': bundle.json_bytes(manifest)})
            self.assert_rejected('bundle mapping:', metric_names=['payload'])

    def test_selected_original_descriptions_preserve_empty_unicode_and_exact_replay(self):
        for description in ('', 'café µ\nhelp \\ text'):
            for strategy in (WITH, WITHOUT):
                for kind, monotonic in (('gauge', None), ('sum', False), ('sum', True)):
                    with self.subTest(description=description, strategy=strategy, kind=kind):
                        source = json.loads(self.repeated_type_source(kind, monotonic))
                        for resource in source['resourceMetrics']:
                            for scope in resource['scopeMetrics']:
                                for metric in scope['metrics']:
                                    if metric['name'] == 'latency':
                                        metric['description'] = description
                        # Source omission and explicit empty both become the empty string.
                        if not description:
                            del source['resourceMetrics'][0]['scopeMetrics'][0]['metrics'][0]['description']
                        source = json.dumps(source).encode()
                        self.create(source, strategy=strategy, convert_unit='ms:s', convert_metrics=['latency'])
                        result = bundle.inspect_bundle(self.output)
                        self.assertEqual(result, bundle.inspect_bundle(self.output))
                        replay = bundle.inspect_bundle(self.output, source=source)
                        self.assertIs(replay.pop('reproduced'), True)
                        self.assertEqual(replay, {k: v for k, v in result.items() if k != 'reproduced'})
                        self.assertEqual([m['sample_count'] for m in result['metrics']], [1, 1, 1, 0])
                        self.assertNotIn('original_description', result['metrics'][0])

    def test_original_descriptions_leave_legacy_and_unselected_validation_unchanged(self):
        source = json.loads(self.repeated_type_source('gauge'))
        source['resourceMetrics'][0]['scopeMetrics'][0]['metrics'][1]['unit'] = 'ms'
        for config in ({}, {'convert_unit': 'ms:s'},
                       {'convert_unit': 'ms:s', 'convert_metrics': ['payload']}):
            members, mapping = self.create(json.dumps(source).encode(), **config)
            before = self.run_cli()
            for value in ('Contradictory help text', None, False, 42, [], {}):
                with self.subTest(config=config, value=value):
                    changed = copy.deepcopy(mapping)
                    del changed['metrics'][0]['original_description']
                    changed['metrics'][3]['original_description'] = value
                    self.write_changed_mapping(members, changed)
                    after = self.run_cli()
                    self.assertEqual((before.returncode, after.returncode), (0, 0), after.stderr)
                    self.assertEqual(before.stdout, after.stdout)

    def test_repeated_original_semantics_preserve_unselected_and_omitted_false(self):
        source = json.loads(self.repeated_type_source('gauge'))
        source['resourceMetrics'][0]['scopeMetrics'][0]['metrics'][1]['unit'] = 'ms'
        source = json.dumps(source).encode()
        for config in ({}, {'convert_unit': 'ms:s'},
                       {'convert_unit': 'ms:s', 'convert_metrics': ['payload']}):
            with self.subTest(config=config):
                members, mapping = self.create(source, **config)
                before = self.run_cli()
                changed = copy.deepcopy(mapping)
                changed['metrics'][3].update(original_type='sum', is_monotonic=False, aggregation_temporality=2)
                self.write_changed_mapping(members, changed)
                after = self.run_cli()
                self.assertEqual((before.returncode, after.returncode, after.stderr), (0, 0, ''))
                self.assertEqual(before.stdout, after.stdout)
        source = json.loads(self.repeated_type_source('sum', False))
        del source['resourceMetrics'][0]['scopeMetrics'][0]['metrics'][0]['sum']['isMonotonic']
        del source['resourceMetrics'][1]['scopeMetrics'][1]['metrics'][0]['sum']['isMonotonic']
        source = json.dumps(source).encode()
        for strategy in (WITH, WITHOUT):
            with self.subTest(strategy=strategy):
                self.create(source, strategy=strategy, convert_unit='ms:s', convert_metrics=['latency'])
                result = bundle.inspect_bundle(self.output)
                self.assertEqual(bundle.inspect_bundle(self.output, source=source), {**result, 'reproduced': True})
                first = self.run_cli()
                self.assertEqual((first.returncode, first.stderr), (0, ''))
                self.assertEqual(first.stdout, self.run_cli().stdout)

    def test_selected_temporality_leaves_legacy_and_unselected_validation_unchanged(self):
        for config in ({}, {'convert_unit': 'ms:s'},
                       {'convert_unit': 'ms:s', 'convert_metrics': ['latency']}):
            with self.subTest(config=config):
                members, mapping = self.create(**config)
                before = self.run_cli()
                self.assertEqual((before.returncode, before.stderr), (0, ''))
                changed = copy.deepcopy(mapping)
                for entry in changed['metrics']:
                    if entry['original_name'] not in config.get('convert_metrics', []):
                        # Deliberately retain the prior no-selection/unselected contract.
                        entry.pop('aggregation_temporality')
                self.write_changed_mapping(members, changed)
                after = self.run_cli()
                self.assertEqual((after.returncode, after.stderr), (0, ''))
                self.assertEqual(after.stdout, before.stdout)

    def test_selected_types_preserve_valid_repeated_empty_and_omitted_sum_monotonicity(self):
        for kind, monotonic in (('gauge', None), ('sum', True), ('sum', False), ('sum', 'omitted')):
            source = self.repeated_type_source(kind, monotonic)
            for strategy in (WITH, WITHOUT):
                with self.subTest(kind=kind, monotonic=monotonic, strategy=strategy):
                    _, mapping = self.create(source, strategy=strategy, convert_unit='ms:s', convert_metrics=['latency'])
                    temporalities = [m['aggregation_temporality'] for m in mapping['metrics']
                                     if m['original_name'] == 'latency']
                    self.assertEqual(temporalities, [None if kind == 'gauge' else 2] * 3)
                    if kind == 'sum':
                        self.assertTrue(all(type(value) is int for value in temporalities))
                    result = bundle.inspect_bundle(self.output, metric_names=['latency'])
                    self.assertEqual((result['sample_count'], result['metric_count'],
                        result['matched_sample_count'], result['matched_metric_count']), (3, 4, 2, 3))
                    self.assertEqual([m['sample_count'] for m in result['metrics']], [1, 1, 0])
                    self.assertEqual([m['target_type'] for m in result['metrics']],
                                     ['counter' if monotonic is True else 'gauge'] * 3)
                    self.assertEqual(bundle.inspect_bundle(self.output, source=source, metric_names=['latency']),
                                     {**result, 'reproduced': True})
                    first = self.run_cli(metric_names=['latency'])
                    self.assertEqual((first.returncode, first.stderr), (0, ''))
                    self.assertEqual(json.loads(first.stdout), result)
                    self.assertEqual(first.stdout, self.run_cli(metric_names=['latency']).stdout)

    def test_selected_naming_preserves_literal_names_and_both_strategy_policies(self):
        source = json.dumps(document(
            gauge(' 9.wait.µ ', 'ms', attrs=[attr('site', 'a')]),
            gauge(' 9.wait.µ ', 'ms', attrs=[attr('site', 'b')]),
            {'name': ' 9.wait.µ ', 'unit': 'ms', 'gauge': {}},
            gauge('wait.milliseconds', 'ms'), gauge('ready.seconds', 'ms'),
            sum_metric('elapsed.milliseconds_total', 'ms'), sum_metric('requests', 'ms'),
            sum_metric('nonmonotonic', 'ms', monotonic=False))).encode()
        selected = [' 9.wait.µ ', 'wait.milliseconds', 'ready.seconds', 'elapsed.milliseconds_total',
                    'requests', 'nonmonotonic']
        for strategy, families in ((WITH, ['_9_wait_seconds'] * 3 + ['wait_seconds', 'ready_seconds',
                'elapsed_seconds_total', 'requests_seconds_total', 'nonmonotonic_seconds']),
                (WITHOUT, ['_9_wait'] * 3 + ['wait_seconds', 'ready_seconds',
                'elapsed_seconds_total', 'requests', 'nonmonotonic'])):
            with self.subTest(strategy=strategy):
                self.create(source, strategy=strategy, convert_unit='ms:s', convert_metrics=selected)
                result = bundle.inspect_bundle(self.output)
                self.assertEqual((result['sample_count'], result['metric_count']), (7, 8))
                self.assertEqual([m['original_name'] for m in result['metrics']], selected[:1] * 3 + selected[1:])
                self.assertEqual([m['sample_count'] for m in result['metrics']], [1, 1, 0, 1, 1, 1, 1, 1])
                self.assertEqual([m['target_family'] for m in result['metrics']], families)
                parser_families = families[:5] + ['elapsed_seconds',
                    'requests_seconds' if strategy == WITH else 'requests', families[7]]
                self.assertEqual([m['parser_family'] for m in result['metrics']], parser_families)
                self.assertEqual([m['parser_sample'] for m in result['metrics']],
                    families[:6] + ['requests_seconds_total' if strategy == WITH else 'requests_total', families[7]])
                self.assertTrue(all((m['original_unit'], m['target_unit'], m['unit_converted']) == ('ms', 's', True)
                                    for m in result['metrics']))
                self.assertEqual(bundle.inspect_bundle(self.output, source=source), {**result, 'reproduced': True})

    def test_reproduction_selected_legacy_empty_and_effective_configuration(self):
        nonfinite = gauge('wait.µ', 'ms')
        nonfinite['gauge']['dataPoints'] = [{'asDouble': 'NaN'}]
        promoted = json.dumps(document(nonfinite,
            resource={'attributes': [attr('service.name', 'demo')]})).encode()
        cases = [(self.source, {}), (self.source, {'convert_unit': 'ms:s'}),
            (self.source, {'convert_unit': 'ms:s', 'convert_metrics': ['latency']}),
            (self.source, {'strategy': WITHOUT, 'convert_unit': 'ms:s', 'convert_metrics': ['latency']}),
            (b'{}', {}), (promoted, {'allow_nonfinite': True, 'promote_resource': ['service.name'],
                                   'convert_unit': 'ms:s', 'convert_metrics': ['wait.µ']})]
        source_path = self.directory / 'input.json'
        for source, config in cases:
            with self.subTest(config=config, source=source[:40]):
                self.create(source, **config)
                source_path.write_bytes(source)
                before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
                result = self.run_cli(source=source_path)
                self.assertEqual((result.returncode, result.stderr), (0, ''))
                integrity = bundle.inspect_bundle(self.output)
                self.assertIs(integrity['reproduced'], False)
                expected = {**integrity, 'reproduced': True}
                self.assertEqual(bundle.inspect_bundle(self.output, source=source), expected)
                self.assertEqual(json.loads(result.stdout), expected)
                self.assertEqual(result.stdout, self.run_cli(source=source_path).stdout)
                self.assertEqual({p.name: p.read_bytes() for p in self.directory.iterdir()}, before)

    def test_reproduction_rejects_changed_or_missing_input_without_summary_or_writes(self):
        self.create(convert_unit='ms:s', convert_metrics=['latency'])
        source_path = self.directory / 'input.json'
        source_path.write_bytes(self.source + b'\n')
        self.assert_rejected('input: SHA-256/size mismatch', source=source_path)
        source_path.unlink()
        self.assert_rejected(source=source_path)
        self.assert_rejected(source=self.directory)

    def test_reproduction_rejects_hash_valid_artifact_and_configuration_changes(self):
        members, _ = self.create(convert_unit='ms:s', convert_metrics=['latency'])
        source_path = self.directory / 'input.json'
        source_path.write_bytes(self.source)
        cases = []
        for name in bundle.ARTIFACTS:
            changed = {**members, name: members[name] + b'\n'}
            manifest = json.loads(members['manifest.json'])
            manifest['artifacts'][name] = bundle.digest(changed[name])
            changed['manifest.json'] = bundle.json_bytes(manifest)
            cases.append((changed, f'{name}: input/configuration did not reproduce stored bytes'))
        for config in ({'convert_metrics': ['elapsed_milliseconds_total']},
                       {'convert_metrics': ['missing']}, {'promote_resource': ['service.name']}):
            manifest = json.loads(members['manifest.json'])
            manifest['configuration'].update(config)
            cases.append(({**members, 'manifest.json': bundle.json_bytes(manifest)},
                          'unmatched exact raw metric names' if config.get('convert_metrics') == ['missing']
                          else 'input/configuration did not reproduce stored bytes'))
        for changed, error in cases:
            with self.subTest(error=error, manifest=changed['manifest.json']):
                bundle.atomic_write_bundle(self.output, changed)
                self.assert_rejected(error, source=source_path)

    def test_reproduction_retains_one_generation_during_read_and_replay(self):
        replacement, _ = bundle.build_bundle(self.source, convert_unit='ms:s', convert_metrics=['latency'])
        real_read, real_build = zipfile.ZipFile.read, bundle.build_bundle

        def replace_after_read(archive, name, *args, **kwargs):
            value = real_read(archive, name, *args, **kwargs)
            if name == 'exposition.prom':
                bundle.atomic_write_bundle(self.output, replacement)
            return value

        def replace_during_replay(*args, **kwargs):
            bundle.atomic_write_bundle(self.output, replacement)
            return real_build(*args, **kwargs)

        for target, side_effect in ((zipfile.ZipFile, replace_after_read), (bundle, replace_during_replay)):
            self.create(b'{}')
            with patch.object(target, 'read' if target is zipfile.ZipFile else 'build_bundle', side_effect), \
                    patch.object(Path, 'open', autospec=True, side_effect=Path.open) as opened:
                result = bundle.inspect_bundle(self.output, source=b'{}')
            self.assertEqual((result['reproduced'], result['sample_count'], result['metrics']), (True, 0, []))
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(bundle.inspect_bundle(self.output)['sample_count'], 6)

    def test_metric_filter_selected_counts_whole_bundle_losses_replay_and_determinism(self):
        self.create(convert_unit='ms:s', convert_metrics=['latency'])
        source_path = self.directory / 'input.json'
        source_path.write_bytes(self.source)
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        for source in (None, source_path):
            with self.subTest(reproduce=source is not None):
                whole = json.loads(self.run_cli(source=source).stdout)
                result = self.run_cli(source=source, metric_names=['latency', 'latency'])
                self.assertEqual((result.returncode, result.stderr), (0, ''))
                expected = {**whole, 'metrics': [whole['metrics'][0]], 'filter_metrics': ['latency'],
                            'matched_sample_count': 2, 'matched_metric_count': 1}
                self.assertEqual(json.loads(result.stdout), expected)
                self.assertEqual(result.stdout, self.run_cli(source=source, metric_names=['latency']).stdout)
                self.assertEqual(bundle.inspect_bundle(self.output,
                    source=self.source if source else None, metric_names=['latency']), expected)
                self.assertEqual((expected['sample_count'], expected['metric_count'], expected['loss_count']), (6, 5, 2))
        self.assertEqual({p.name: p.read_bytes() for p in self.directory.iterdir()}, before)

    def test_metric_filter_literal_names_all_repeated_and_empty_occurrences(self):
        raw = ' Wait.é* '  # Spaces, Unicode, punctuation and glob metacharacters are literal.
        source = json.dumps({'resourceMetrics': [
            {'scopeMetrics': [{'metrics': [gauge(raw, 'ms', attrs=[attr('site', 'a')]),
                                          gauge('other', 'By')]}]},
            {'scopeMetrics': [{'metrics': [gauge(raw, 'ms', attrs=[attr('site', 'b')])]},
                              {'metrics': [{'name': raw, 'unit': 'ms', 'gauge': {}}]}]}]}).encode()
        for config in ({}, {'strategy': WITHOUT}, {'convert_unit': 'ms:s', 'convert_metrics': [raw]}):
            with self.subTest(config=config):
                self.create(source, **config)
                whole = bundle.inspect_bundle(self.output)
                result = self.run_cli(metric_names=[raw, raw])
                self.assertEqual((result.returncode, result.stderr), (0, ''))
                summary = json.loads(result.stdout)
                self.assertEqual(summary, {**whole, 'metrics': [whole['metrics'][i] for i in (0, 2, 3)],
                    'filter_metrics': [raw], 'matched_sample_count': 2, 'matched_metric_count': 3})
                self.assertEqual([m['sample_count'] for m in summary['metrics']], [1, 1, 0])
                for names in ([raw, 'other', raw], ('other', raw), {raw, 'other'}, frozenset([raw, 'other'])):
                    combined = bundle.inspect_bundle(self.output, source=source, metric_names=names)
                    self.assertEqual(combined, {**whole, 'reproduced': True, 'filter_metrics': sorted([raw, 'other']),
                        'matched_sample_count': 3, 'matched_metric_count': 4})
                for name in (raw.strip(), raw.lower(), ' Wait.e\u0301* ', 'Wait_é', ' Wait.* ', ' Wait.é. ',
                             whole['metrics'][0]['target_family']):
                    self.assert_rejected('unmatched exact raw metric names', metric_names=[raw, name])
        self.create(json.dumps(document({'name': 'empty', 'gauge': {}})).encode())
        summary = bundle.inspect_bundle(self.output, metric_names=['empty'])
        self.assertEqual((summary['matched_sample_count'], summary['matched_metric_count']), (0, 1))

    def test_metric_filter_unmatched_and_invalid_requests_fail_without_writes(self):
        self.create(convert_unit='ms:s', convert_metrics=['latency'])
        for name in ('Latency', ' latency ', 'latency_seconds', 'latency*', 'latenc.', ''):
            with self.subTest(name=name):
                self.assert_rejected(metric_names=['latency', name])
        for names in ('latency', {'latency': 1}, 1, True, [], (), set(), frozenset(), [''], [None], ['latency', 1], ['\ud800']):
            with self.subTest(names=repr(names)):
                with self.assertRaises(ConversionError):
                    bundle.inspect_bundle(self.output, metric_names=names)
        self.create(b'{}')
        self.assert_rejected('unmatched exact raw metric names', metric_names=['latency'])

    def test_metric_filter_cannot_hide_bad_artifacts_descriptors_totals_losses_or_replay(self):
        members, mapping = self.create(convert_unit='ms:s', convert_metrics=['latency'])
        # Filter for a nonconverted metric; invalid selected descriptors must still fail.
        for key, value in (('target_family', 'wrong'), ('original_unit', 's'), ('data_points', None)):
            changed = copy.deepcopy(mapping)
            changed['metrics'][0][key] = value
            self.write_changed_mapping(members, changed)
            self.assert_rejected('mapping', metric_names=['payload'])
        for changed in ({**mapping, 'sample_count': 7}, {**mapping, 'losses': [None]}):
            self.write_changed_mapping(members, changed)
            self.assert_rejected('mapping', metric_names=['latency'])
        changed = copy.deepcopy(mapping)
        changed['metrics'][1]['parser_sample'] = None  # Unmatched descriptor is also validated.
        self.write_changed_mapping(members, changed)
        self.assert_rejected('mapping', metric_names=['latency'])
        for name in bundle.ARTIFACTS:
            bundle.atomic_write_bundle(self.output, {**members, name: b'!' + members[name]})
            self.assert_rejected(f'{name}: SHA-256/size mismatch', metric_names=['latency'])
        source_path = self.directory / 'input.json'
        source_path.write_bytes(self.source)
        changed = {**members, 'exposition.prom': members['exposition.prom'].replace(b'9223372036854775807', b'9223372036854775806')}
        manifest = json.loads(members['manifest.json'])
        manifest['artifacts']['exposition.prom'] = bundle.digest(changed['exposition.prom'])
        changed['manifest.json'] = bundle.json_bytes(manifest)
        bundle.atomic_write_bundle(self.output, changed)
        self.assert_rejected('did not reproduce stored bytes', source=source_path, metric_names=['latency'])
        bundle.atomic_write_bundle(self.output, members)
        source_path.write_bytes(self.source + b'\n')
        self.assert_rejected('input: SHA-256/size mismatch', source=source_path, metric_names=['latency'])

    def write_changed_mapping(self, members, mapping):
        raw = bundle.json_bytes(mapping)
        manifest = json.loads(members['manifest.json'])
        manifest['artifacts']['mapping.json'] = bundle.digest(raw)
        bundle.atomic_write_bundle(self.output, {**members, 'mapping.json': raw,
                                                'manifest.json': bundle.json_bytes(manifest)})

    def test_metric_filter_retains_verified_generation_during_read_and_replay(self):
        replacement, _ = bundle.build_bundle(b'{}')
        real_read, real_build = zipfile.ZipFile.read, bundle.build_bundle

        def replace_after_read(archive, name, *args, **kwargs):
            value = real_read(archive, name, *args, **kwargs)
            if name == 'exposition.prom':
                bundle.atomic_write_bundle(self.output, replacement)
            return value

        def replace_during_replay(*args, **kwargs):
            bundle.atomic_write_bundle(self.output, replacement)
            return real_build(*args, **kwargs)

        for target, method, side_effect in ((zipfile.ZipFile, 'read', replace_after_read),
                                             (bundle, 'build_bundle', replace_during_replay)):
            for source in ((None, self.source) if target is zipfile.ZipFile else (self.source,)):
                with self.subTest(method=method, reproduce=source is not None):
                    self.create(convert_unit='ms:s', convert_metrics=['latency'])
                    with patch.object(target, method, side_effect), \
                            patch.object(Path, 'open', autospec=True, side_effect=Path.open) as opened:
                        result = bundle.inspect_bundle(self.output, source=source, metric_names=['latency'])
                    self.assertEqual(opened.call_count, 1)
                    self.assertEqual((result['sample_count'], result['matched_sample_count'],
                                      result['matched_metric_count']), (6, 2, 1))
                    self.assertEqual([m['original_name'] for m in result['metrics']], ['latency'])
                    self.assertIs(result['reproduced'], source is not None)
                    self.assertEqual(bundle.inspect_bundle(self.output)['sample_count'], 0)
