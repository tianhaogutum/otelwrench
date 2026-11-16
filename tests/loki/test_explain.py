"""Selected explanations must share preview's actual projection and rejection."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from .helpers import ROOT, attr, logs, profile
from otelwrench.loki.config import load_profile
from otelwrench.loki import mapping
from otelwrench.loki.strict import PreviewError, read_json

FIXTURE = ROOT / 'examples/loki/explain'
RECORD = 'resourceLogs[0].scopeLogs[0].logRecords[0]'
RESOURCE_RULES = '/limits_config/otlp_config/resource_attributes/attributes_config/'
LOG_RULES = '/limits_config/otlp_config/log_attributes/'


class ExplainTests(unittest.TestCase):
    def setUp(self):
        self.data = read_json(FIXTURE / 'logs.json')
        self.policy = load_profile(FIXTURE / 'profile.yaml')

    def explain(self, data=None, policy=None, **selection):
        return mapping.explain(self.data if data is None else data,
                               self.policy if policy is None else policy,
                               record_id=RECORD, **selection)

    def test_literal_and_re2_precedence_before_normalization(self):
        report = self.explain(key='service_name')
        self.assertEqual(report['record'], mapping.preview(self.data, self.policy)['records'][0])
        self.assertEqual(len(report['explanations']), 1)
        trace = report['explanations'][0]
        self.assertEqual(trace['sourceRef'], {'recordId': RECORD, 'source': 'resource',
                                              'sourcePath': ['service.name']})
        self.assertEqual(trace['normalization'], {'joinedKey': 'service.name', 'normalizedKey': 'service_name'})
        self.assertEqual(trace['final'], report['record']['placements'][0])
        decision = trace['selection']
        self.assertEqual(decision['originalKey'], 'service.name')
        self.assertEqual(decision['ruleRef'], RESOURCE_RULES + '0')
        self.assertEqual([(r['ruleRef'], r['matched'], r['status']) for r in decision['rules']], [
            (RESOURCE_RULES + '0', True, 'selected'),
            (RESOURCE_RULES + '1', True, 'shadowed'),
            (RESOURCE_RULES + '2', False, 'not_matched')])
        self.assertEqual(decision['rules'][0]['selector'], {'attributes': ['service.name']})
        self.assertEqual(decision['rules'][1]['selector'], {'regex': '^service[.]'})
        self.assertEqual(decision['rules'][1]['matchType'], 're2_search')
        self.assertTrue(decision['defaultResourceList']['containsKey'])
        self.assertFalse(decision['defaultResourceList']['selected'])
        self.assertFalse(decision['fallback']['selected'])
        self.assertEqual(report, self.explain(key='service_name'))

    def test_nested_leaves_share_top_level_rule_and_keep_exact_paths(self):
        report = self.explain(key='net_peer_port')
        trace = report['explanations'][0]
        self.assertEqual(trace['sourceRef']['sourcePath'], ['net.peer', 'port'])
        self.assertEqual(trace['normalization'], {'joinedKey': 'net.peer_port', 'normalizedKey': 'net_peer_port'})
        self.assertEqual(trace['selection']['originalKey'], 'net.peer')
        self.assertEqual(trace['selection']['ruleRef'], LOG_RULES + '0')
        self.assertEqual([r['status'] for r in trace['selection']['rules']],
                         ['selected', 'shadowed', 'not_matched'])
        self.assertEqual(trace['final']['value'], '443')
        self.assertEqual(trace['final']['placement'], 'structured_metadata')
        # Different segmentations that join to the same key remain distinguishable.
        candidate = logs(log=[attr('a_b', {'kvlistValue': {'values': [attr('c')]}})])
        trace = self.explain(candidate, profile(), key='a_b_c')['explanations'][0]
        self.assertEqual(trace['sourceRef']['sourcePath'], ['a_b', 'c'])

    def test_default_fallback_fixed_fields_and_all_final_placements(self):
        report = self.explain()
        by_key = {t['final']['key']: t for t in report['explanations']}
        self.assertEqual(len(by_key), len(report['record']['placements']))
        pod = by_key['k8s_pod_name']
        self.assertEqual(pod['final']['placement'], 'index_label')
        self.assertTrue(pod['selection']['defaultResourceList']['selected'])
        self.assertTrue(pod['selection']['ruleRef'].startswith('defaults.json#/attributes/'))
        empty = by_key['empty']
        self.assertEqual(empty['final']['value'], '')
        self.assertTrue(empty['selection']['fallback']['selected'])
        self.assertEqual(empty['selection']['ruleRef'], 'builtin:structured_metadata')
        self.assertEqual(by_key['debug_private_value']['selection']['ruleRef'], LOG_RULES + '2')
        self.assertEqual(by_key['debug_private_value']['final']['placement'], 'drop')
        self.assertEqual(by_key['internal_component']['selection']['ruleRef'],
                         '/limits_config/otlp_config/scope_attributes/0')
        fixed = by_key['scope_name']
        self.assertEqual(fixed['selection']['ruleRef'], 'builtin:fixed_otlp_field')
        self.assertEqual(fixed['selection']['rules'], [])
        self.assertEqual(fixed['final']['placement'], 'structured_metadata')
        ignored = self.explain(policy=profile(resource_attributes={'ignore_defaults': True}), key='service_name')
        trace = ignored['explanations'][0]
        self.assertFalse(trace['selection']['defaultResourceList']['enabled'])
        self.assertTrue(trace['selection']['fallback']['selected'])
        self.assertEqual(ignored['record']['stream'], {})
        self.assertTrue(any('no nonempty index label' in w for w in ignored['warnings']))

    def test_invalid_projection_never_becomes_a_partial_explanation(self):
        collision = logs(log=[attr('parent', {'kvlistValue': {'values': [attr('child')]}}),
                              attr('parent_child')])
        fixed_collision = logs(log=[attr('severity.text')], severityText='INFO')
        for data, policy, message in (
            (collision, profile(), 'normalized key collision'),
            (collision, profile(log_attributes=[{'action': 'drop', 'regex': '.*'}]), 'normalized key collision'),
            (fixed_collision, profile(), 'normalized key collision'),
            (logs(resource=[attr('service.name', {})]), profile(), 'empty index-label value')):
            with self.subTest(message=message):
                errors = []
                for operation in (lambda: mapping.preview(data, policy), lambda: self.explain(data, policy)):
                    with self.assertRaises(PreviewError) as raised:
                        operation()
                    errors.append(str(raised.exception))
                self.assertEqual(errors[0], errors[1])
                self.assertIn(message, errors[0])
                self.assertIn(RECORD, errors[0])
                if message == 'empty index-label value':
                    self.assertIn("resource:['service.name']", errors[0])
                    self.assertIn('defaults.json#/attributes/', errors[0])
                else:
                    self.assertIn('log:', errors[0])
        # An invalid later record cannot be hidden by selecting the first one.
        data = copy.deepcopy(self.data)
        data['resourceLogs'][0]['scopeLogs'][0]['logRecords'].append({'timeUnixNano': '0'})
        with self.assertRaisesRegex(PreviewError, 'missing timestamp'):
            self.explain(data, key='service_name')

    def test_empty_label_can_be_explicitly_demoted_or_dropped(self):
        data = logs(resource=[attr('service.name', {})])
        for action in ('structured_metadata', 'drop'):
            policy = profile(resource_attributes={'attributes_config': [
                {'action': action, 'attributes': ['service.name']}]})
            trace = self.explain(data, policy, key='service_name')['explanations'][0]
            self.assertEqual(trace['selection']['ruleRef'], RESOURCE_RULES + '0')
            self.assertEqual(trace['final']['placement'], action)
            self.assertEqual(trace['final']['value'], '')

    def test_missing_record_or_key_is_an_error(self):
        for record_id, key in [('missing', None), (RECORD, 'net.peer'), (RECORD, '')]:
            with self.subTest(record_id=record_id, key=key), self.assertRaises(PreviewError):
                mapping.explain(self.data, self.policy, record_id=record_id, key=key)
        with self.assertRaises(PreviewError):
            mapping.explain({'resourceLogs': []}, profile(), record_id=RECORD)

    def test_exact_record_selection_and_leading_digit_normalization(self):
        data = logs(log=[attr('9.parent', {'kvlistValue': {'values': [attr('child-key')]}})])
        second = copy.deepcopy(data['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0])
        second['timeUnixNano'] = '1700000000000000002'
        data['resourceLogs'][0]['scopeLogs'][0]['logRecords'].append(second)
        record_id = RECORD.replace('logRecords[0]', 'logRecords[1]')
        report = mapping.explain(data, profile(), record_id=record_id, key='_9_parent_child_key')
        self.assertEqual(report['record'], mapping.preview(data, profile())['records'][1])
        trace = report['explanations'][0]
        self.assertEqual(trace['sourceRef'], {'recordId': record_id, 'source': 'log',
                                             'sourcePath': ['9.parent', 'child-key']})
        self.assertEqual(trace['normalization'], {'joinedKey': '9.parent_child-key',
                                                  'normalizedKey': '_9_parent_child_key'})

    def test_cli_selection_strict_config_and_no_output_capability(self):
        command = [sys.executable, '-m', 'otelwrench', 'loki', 'explain', str(FIXTURE / 'logs.json'),
                   '--config', str(FIXTURE / 'profile.yaml'), '--record-id', RECORD, '--key', 'net_peer_port']
        result = subprocess.run(command, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.explain(key='net_peer_port'))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'push.json'
            for extra in (['--push-json', str(output)], ['--endpoint', 'https://example.invalid']):
                result = subprocess.run(command + extra, text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertFalse(output.exists())
            bad = Path(directory) / 'invalid.yaml'
            invalid_profiles = [
                'limits_config: {otlp_config: {log_attributes: [{action: drop, regex: "(?=x)"}]}}',
                'limits_config: {otlp_config: {log_attributes: [{action: index_label, attributes: [x]}]}}',
                'limits_config: {otlp_config: {unknown: true}}',
                'limits_config: {}\nlimits_config: {}\n',
            ]
            for content in invalid_profiles:
                bad.write_text(content)
                candidate = list(command)
                candidate[6] = str(bad)
                result = subprocess.run(candidate, text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
