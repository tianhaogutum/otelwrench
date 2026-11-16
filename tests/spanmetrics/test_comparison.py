# SPDX-License-Identifier: Apache-2.0
"""Hand-counted synthetic capture; no production cardinality extrapolation."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from .test_core import document, encode, span, samples
from otelwrench.spanmetrics.core import Config, InputError, convert, parse_dimensions

ROOT = Path(__file__).resolve().parents[2]
BASELINE = Config(dimensions=parse_dimensions(['span:method=method']))
CANDIDATE = Config(dimensions=parse_dimensions([
    'span:method=method', 'resource:zone=zone', 'span:customer=customer']))


def compare(data, baseline=BASELINE, candidate=CANDIDATE):
    from otelwrench.spanmetrics.comparison import compare as implementation
    return implementation(data, baseline, candidate)


class ComparisonTests(unittest.TestCase):
    def test_hand_counts_and_source_combinations_match_independent_metric_parser(self):
        data = (ROOT / 'examples/spanmetrics/dimensions.json').read_bytes()
        result = compare(data)
        self.assertTrue(result['comparison_complete'])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['unique_records'], 6)
        self.assertEqual(result['series_delta'], 24)
        for name, config, groups, series in [('baseline', BASELINE, 2, 16), ('candidate', CANDIDATE, 5, 40)]:
            summary = result['configurations'][name]
            self.assertEqual((summary['groups'], summary['series']), (groups, series))
            self.assertEqual(summary['validation']['duplicate_records'], 1)
            parsed = samples(convert(data, config))
            self.assertEqual(len(parsed), series)
            identities = {(s.name, tuple(sorted(s.labels.items()))) for s in parsed}
            self.assertEqual(len(identities), series)
            calls = {tuple(sorted(s.labels.items())): int(s.value) for s in parsed if s.name == 'spanmetrics_calls_total'}
            observed = {}
            for partition in result['partitions']:
                for combo in partition['attribute_combinations']:
                    key = tuple(sorted(combo[name + '_labels'].items()))
                    observed[key] = observed.get(key, 0) + combo['unique_records']
            self.assertEqual(observed, calls)
        partitions = {p['shared_attributes'][0]['value']: p for p in result['partitions']}
        self.assertEqual(set(partitions), {'s:GET', 's:POST'})
        self.assertEqual([(partitions[m]['baseline_groups'], partitions[m]['candidate_groups'], partitions[m]['series_delta'])
                          for m in ('s:GET', 's:POST')], [(1, 3, 16), (1, 2, 8)])
        # Independently enumerated joint source combinations, preserving resource/span and scalar type.
        observed = {}
        for partition in result['partitions']:
            for combo in partition['attribute_combinations']:
                values = {(a['source'], a['key']): a['value'] for a in combo['source_attributes']}
                observed[(values['resource', 'zone'], values['span', 'method'], values['span', 'customer'])] = combo['unique_records']
        self.assertEqual(observed, {('s:east', 's:GET', 's:1'): 1, ('s:east', 's:GET', 'i:1'): 1,
                                    ('s:west', 's:GET', 'missing'): 2, ('s:east', 's:POST', 's:a|b'): 1,
                                    ('s:west', 's:POST', 's:missing'): 1})
        self.assertFalse(result['metrics_emitted'])
        self.assertIn('capture', result['scope'])

    def test_arbitrary_configs_and_renamed_common_sources_are_compared(self):
        data = (ROOT / 'examples/spanmetrics/dimensions.json').read_bytes()
        baseline = Config(dimensions=parse_dimensions(['resource:zone=location']))
        candidate = Config(dimensions=parse_dimensions(['span:customer=customer']))
        result = compare(data, baseline, candidate)
        self.assertEqual(result['series_delta'], 24)  # 2 zones vs 5 typed customers
        self.assertEqual(len(result['partitions']), 1)
        self.assertEqual(result['partitions'][0]['shared_attributes'], [])
        reverse = compare(data, candidate, baseline)
        self.assertEqual(reverse['series_delta'], -24)
        renamed = replace(BASELINE, dimensions=parse_dimensions(['span:method=verb']))
        result = compare(data, BASELINE, renamed)
        self.assertEqual(result['series_delta'], 0)
        self.assertEqual(len(result['partitions']), 2)

    def test_structured_identities_do_not_collide_on_delimiters_or_sources(self):
        records = [span(1, attributes=[{'key': 'a', 'value': {'stringValue': 'x|s:y'}},
                                       {'key': 'b', 'value': {'stringValue': 'z'}}]),
                   span(2, attributes=[{'key': 'a', 'value': {'stringValue': 'x'}},
                                       {'key': 'b', 'value': {'stringValue': 'y|s:z'}}])]
        config = Config(dimensions=parse_dimensions(['span:a=a', 'span:b=b', 'resource:a=resource_a']))
        result = compare(encode(document(records)), Config(), config)
        self.assertEqual(result['configurations']['candidate']['groups'], 2)
        combos = result['partitions'][0]['attribute_combinations']
        self.assertEqual(len(combos), 2)
        for combo in combos:
            self.assertEqual(combo['candidate_labels']['resource_a'], 'missing')
            self.assertEqual(len(combo['source_attributes']), 3)

    def test_same_label_from_different_sources_is_not_a_shared_dimension(self):
        resources = [{'key': 'service.name', 'value': {'stringValue': 'web'}},
                     {'key': 'zone', 'value': {'stringValue': 'east'}}]
        records = [span(i, attributes=[{'key': 'zone', 'value': {'stringValue': value}}])
                   for i, value in enumerate(('east', 'west'), 1)]
        baseline = Config(dimensions=parse_dimensions(['resource:zone=zone']))
        candidate = Config(dimensions=parse_dimensions(['span:zone=zone']))
        result = compare(encode(document(records, resources)), baseline, candidate)
        self.assertEqual(result['series_delta'], 8)
        self.assertEqual(len(result['partitions']), 1)
        partition = result['partitions'][0]
        self.assertEqual(partition['shared_attributes'], [])
        self.assertEqual((partition['baseline_groups'], partition['candidate_groups']), (1, 2))
        self.assertEqual([c['source_attributes'] for c in partition['attribute_combinations']], [
            [{'source': 'resource', 'key': 'zone', 'value': 's:east'},
             {'source': 'span', 'key': 'zone', 'value': 's:east'}],
            [{'source': 'resource', 'key': 'zone', 'value': 's:east'},
             {'source': 'span', 'key': 'zone', 'value': 's:west'}]])

    def test_mandatory_label_groups_stay_separate_in_attribution(self):
        records = [span(1), span(2, name='other'), span(3, kind=3), span(4, status={'code': 2})]
        result = compare(encode(document(records)), Config(), Config())
        self.assertEqual(result['configurations']['baseline']['groups'], 4)
        self.assertEqual(len(result['partitions']), 4)
        for partition in result['partitions']:
            self.assertEqual((partition['baseline_groups'], partition['candidate_groups']), (1, 1))
            self.assertEqual(partition['series_delta'], 0)

    def test_rejections_suppress_all_comparison_counts_even_if_one_config_passes(self):
        doc = json.loads((ROOT / 'examples/spanmetrics/dimensions.json').read_text())
        variants = []
        invalid = deepcopy(doc)
        invalid['resourceSpans'][0]['scopeSpans'][0]['spans'][0]['name'] = ''
        variants.append((encode(invalid), BASELINE, CANDIDATE))
        complex_value = deepcopy(doc)
        complex_value['resourceSpans'][0]['scopeSpans'][0]['spans'][0]['attributes'][1]['value'] = {'arrayValue': {}}
        variants.append((encode(complex_value), BASELINE, CANDIDATE))
        variants.extend([(encode(doc), replace(BASELINE, max_series=16), replace(CANDIDATE, max_series=16)),
                         (encode(doc), replace(BASELINE, max_spans=2), replace(CANDIDATE, max_spans=2)),
                         (encode(doc), replace(BASELINE, max_input_bytes=10), replace(CANDIDATE, max_input_bytes=10)),
                         (b'{', BASELINE, CANDIDATE)])
        conflict = deepcopy(doc)
        records = conflict['resourceSpans'][0]['scopeSpans'][0]['spans']
        records.append(dict(records[0], name='conflict'))
        variants.append((encode(conflict), BASELINE, CANDIDATE))
        for data, baseline, candidate in variants:
            with self.subTest(data=data[:50], baseline=baseline):
                result = compare(data, baseline, candidate)
                self.assertEqual(result['status'], 'rejected')
                self.assertFalse(result['comparison_complete'])
                self.assertIsNone(result['unique_records'])
                self.assertIsNone(result['series_delta'])
                self.assertEqual(result['partitions'], [])
                for config in result['configurations'].values():
                    self.assertIsNone(config['groups'])
                    self.assertIsNone(config['series'])
                self.assertFalse(result['metrics_emitted'])

    def test_empty_capture_and_identical_configs(self):
        result = compare(b'{"resourceSpans": []}')
        self.assertTrue(result['comparison_complete'])
        self.assertEqual(result['unique_records'], 0)
        self.assertEqual(result['series_delta'], 0)
        self.assertEqual(result['partitions'], [])
        data = (ROOT / 'examples/spanmetrics/dimensions.json').read_bytes()
        result = compare(data, CANDIDATE, CANDIDATE)
        self.assertEqual(result['series_delta'], 0)
        self.assertTrue(all(p['series_delta'] == 0 for p in result['partitions']))

    def test_report_deterministic_for_unique_record_and_envelope_order(self):
        doc = json.loads((ROOT / 'examples/spanmetrics/dimensions.json').read_text())
        first = compare(encode(doc))
        doc['resourceSpans'].reverse()
        for resource in doc['resourceSpans']:
            resource['scopeSpans'][0]['spans'].reverse()
        second = compare(encode(doc))
        self.assertEqual(first['partitions'], second['partitions'])

    def test_mismatched_limits_or_buckets_are_not_dimension_only_comparisons(self):
        for changed in (replace(CANDIDATE, max_series=100), replace(CANDIDATE, max_spans=100),
                        replace(CANDIDATE, max_input_bytes=100), replace(CANDIDATE, buckets=(1,))):
            with self.subTest(changed=changed), self.assertRaises(InputError):
                compare(b'{"resourceSpans": []}', BASELINE, changed)
