"""Observed partition changes, including zero net stream-count change."""

import copy
import unittest

from .helpers import ROOT, attr, logs, profile
from otelwrench.loki.config import load_profile
from otelwrench.loki.mapping import compare, preview
from otelwrench.loki.strict import read_json

FIXTURE = ROOT / 'examples/loki/stream-topology'


def rid(resource, record=0):
    return f'resourceLogs[{resource}].scopeLogs[0].logRecords[{record}]'


class StreamTopologyTests(unittest.TestCase):
    def setUp(self):
        self.data = read_json(FIXTURE / 'logs.json')
        self.before = load_profile(FIXTURE / 'before.yaml')
        self.after = load_profile(FIXTURE / 'after.yaml')
        self.no_labels = profile(resource_attributes={'ignore_defaults': True})

    def check_conservation(self, report, data, before, after):
        topology = report['streamTopology']
        for side, policy in [('before', before), ('after', after)]:
            projected = preview(data, policy)
            self.assertEqual(topology[side], projected['streams'])
            self.assertCountEqual([r for s in topology[side] for r in s['recordIds']],
                                  [r['id'] for r in projected['records']])
        transitions = topology['transitions']
        self.assertCountEqual([r for t in transitions for r in t['recordIds']],
                              [r['id'] for r in preview(data, before)['records']])
        self.assertEqual([(t['beforeStreamIndex'], t['afterStreamIndex']) for t in transitions],
                         sorted((t['beforeStreamIndex'], t['afterStreamIndex']) for t in transitions))
        for t in transitions:
            self.assertEqual(t['recordCount'], len(t['recordIds']))
            self.assertGreater(t['recordCount'], 0)
            self.assertEqual(set(t['recordIds']),
                             set(topology['before'][t['beforeStreamIndex']]['recordIds']) &
                             set(topology['after'][t['afterStreamIndex']]['recordIds']))

    def test_crossing_partitions_report_both_merges_and_splits_at_zero_delta(self):
        result = compare(self.data, self.before, self.after)
        self.assertEqual(result['streamCountDelta'], 0)
        self.assertEqual(result['changedPlacementCount'], 8)
        self.assertEqual(result['streamTopology'], read_json(FIXTURE / 'expected-topology.json'))
        self.assertNotIn('records', result)
        detailed = compare(self.data, self.before, self.after, include_records=True)
        self.assertEqual(detailed['streamTopology'], result['streamTopology'])
        self.check_conservation(result, self.data, self.before, self.after)

    def test_disjoint_merge_and_split_cancel_in_total_count(self):
        # A+B merge into U while C splits into V+W: neither event may disappear.
        for resource, old, new in zip(self.data['resourceLogs'], ['a', 'b', 'c', 'c'], ['u', 'u', 'v', 'w']):
            resource['resource']['attributes'][0]['value'] = {'stringValue': old}
            resource['resource']['attributes'][1]['value'] = {'stringValue': new}
        result = compare(self.data, self.before, self.after)
        self.assertEqual(result['streamCountDelta'], 0)
        topology = result['streamTopology']
        self.assertEqual(topology['merges'], [{'afterStreamIndex': 0, 'beforeStreamIndexes': [0, 1]}])
        self.assertEqual(topology['splits'], [{'beforeStreamIndex': 2, 'afterStreamIndexes': [1, 2]}])
        self.assertTrue(topology['membershipChanged'])
        self.check_conservation(result, self.data, self.before, self.after)

    def test_merge_reverse_split_preserve_duplicate_records_and_input_order(self):
        # Same body/time and multiple records in one resource must not be deduplicated.
        records = self.data['resourceLogs'][0]['scopeLogs'][0]['logRecords']
        records.append(copy.deepcopy(records[0]))
        result = compare(self.data, self.before, self.no_labels)
        topology = result['streamTopology']
        ids = [rid(0), rid(0, 1), rid(1), rid(2), rid(3)]
        self.assertEqual(result['streamCountDelta'], -1)
        self.assertEqual(topology['after'], [{'stream': {}, 'recordIds': ids, 'recordCount': 5}])
        self.assertEqual(topology['transitions'], [
            {'beforeStreamIndex': 0, 'afterStreamIndex': 0, 'recordIds': ids[:3], 'recordCount': 3},
            {'beforeStreamIndex': 1, 'afterStreamIndex': 0, 'recordIds': ids[3:], 'recordCount': 2}])
        self.assertEqual(topology['merges'], [{'afterStreamIndex': 0, 'beforeStreamIndexes': [0, 1]}])
        self.assertEqual(topology['splits'], [])
        self.assertTrue(topology['membershipChanged'])
        self.assertTrue(any('no nonempty index label' in w for w in result['warnings']))
        reverse = compare(self.data, self.no_labels, self.before)['streamTopology']
        self.assertEqual(reverse['before'], topology['after'])
        self.assertEqual(reverse['after'], topology['before'])
        self.assertEqual(reverse['merges'], [])
        self.assertEqual(reverse['splits'], [{'beforeStreamIndex': 0, 'afterStreamIndexes': [0, 1]}])
        self.check_conservation(result, self.data, self.before, self.no_labels)

    def test_one_to_one_relabel_is_not_membership_change(self):
        self.data['resourceLogs'] = [self.data['resourceLogs'][0], self.data['resourceLogs'][3]]
        result = compare(self.data, self.before, self.after)
        self.assertEqual(len(result['changedStreams']), 2)
        topology = result['streamTopology']
        self.assertFalse(topology['membershipChanged'])
        self.assertEqual(topology['merges'], [])
        self.assertEqual(topology['splits'], [])
        self.check_conservation(result, self.data, self.before, self.after)

    def test_identity_metadata_drop_and_reason_only_changes_do_not_regroup(self):
        drop = profile(resource_attributes={'ignore_defaults': True, 'attributes_config': [
            {'action': 'index_label', 'attributes': ['route.old']},
            {'action': 'drop', 'attributes': ['route.new']}]})
        regex = profile(resource_attributes={'ignore_defaults': True, 'attributes_config': [
            {'action': 'index_label', 'regex': '^route[.]old$'}]})
        for after, placements in [(self.before, 0), (drop, 4), (regex, 0)]:
            with self.subTest(profile=after):
                result = compare(self.data, self.before, after)
                topology = result['streamTopology']
                self.assertEqual(topology['before'], topology['after'])
                self.assertFalse(topology['membershipChanged'])
                self.assertEqual(topology['merges'], [])
                self.assertEqual(topology['splits'], [])
                self.assertEqual(len(topology['transitions']), 2)
                self.assertEqual(result['changedPlacementCount'], placements)
                self.assertEqual(result['changedStreams'], [])
                self.check_conservation(result, self.data, self.before, after)

    def test_empty_capture_and_no_label_identity(self):
        result = compare({'resourceLogs': []}, self.before, self.after)
        self.assertEqual(result['streamTopology'], {
            'before': [], 'after': [], 'transitions': [], 'merges': [], 'splits': [], 'membershipChanged': False})
        result = compare(self.data, self.no_labels, self.no_labels)
        topology = result['streamTopology']
        self.assertFalse(topology['membershipChanged'])
        self.assertEqual(topology['before'], [{'stream': {}, 'recordIds': [rid(i) for i in range(4)], 'recordCount': 4}])
        self.check_conservation(result, self.data, self.no_labels, self.no_labels)

    def test_stream_indexes_follow_label_sort_not_first_occurrence(self):
        self.data['resourceLogs'].reverse()
        # Attribute order also must not affect label identity or stream numbering.
        for resource in self.data['resourceLogs']:
            resource['resource']['attributes'].reverse()
        result = compare(self.data, self.before, self.after)
        topology = result['streamTopology']
        self.assertEqual([s['stream'] for s in topology['before']], [{'route_old': 'x'}, {'route_old': 'y'}])
        self.assertEqual([s['recordIds'] for s in topology['before']], [[rid(2), rid(3)], [rid(0), rid(1)]])
        self.assertEqual(topology['transitions'][0]['recordIds'], [rid(3)])
        self.check_conservation(result, self.data, self.before, self.after)

    def test_stream_identity_includes_label_keys_and_nested_normalization(self):
        # Equal values on different label keys must remain distinct observed streams.
        data = logs(resource=[attr('route.old', {'stringValue': 'x'})])
        data['resourceLogs'].append(copy.deepcopy(data['resourceLogs'][0]))
        data['resourceLogs'][1]['resource']['attributes'] = [attr('route', {'kvlistValue': {'values': [
            attr('new', {'stringValue': 'x'})]}})]
        indexed = profile(resource_attributes={'ignore_defaults': True, 'attributes_config': [
            {'action': 'index_label', 'attributes': ['route.old', 'route']}]})
        result = compare(data, indexed, self.no_labels)
        self.assertEqual([s['stream'] for s in result['streamTopology']['before']],
                         [{'route_new': 'x'}, {'route_old': 'x'}])
        self.assertEqual(result['streamTopology']['merges'], [{'afterStreamIndex': 0, 'beforeStreamIndexes': [0, 1]}])
        self.check_conservation(result, data, indexed, self.no_labels)
