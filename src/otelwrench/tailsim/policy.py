"""Strict Collector policy subset; full-trace deterministic evaluation."""

from dataclasses import dataclass
from pathlib import Path

import yaml

from .validation import InputError, array, integer, obj, string


class StrictLoader(yaml.SafeLoader):
    """Safe YAML without ambiguous duplicate keys or alias graphs."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise InputError("YAML aliases are unsupported; write policy entries explicitly")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise InputError("YAML mapping keys must be strings")
            if key in result:
                raise InputError(f"YAML: duplicate key {key!r}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


# Match Collector's permitted nesting, rather than inventing recursive policy syntax.
LEAVES = {'always_sample', 'status_code', 'latency', 'string_attribute'}


@dataclass(frozen=True)
class Policy:
    name: str
    kind: str
    params: dict
    children: tuple = ()

    def evaluate(self, trace):
        children = [p.evaluate(trace) for p in self.children]
        if self.kind == 'always_sample':
            matched, reason = True, 'always_sample'
        elif self.kind == 'status_code':
            codes = sorted(trace.statuses & set(self.params['status_codes']))
            matched, reason = bool(codes), f"matching observed statuses: {codes}"
        elif self.kind == 'latency':
            ms = trace.duration_ns // 1_000_000
            lower = self.params['threshold_ms']
            upper = self.params.get('upper_threshold_ms', 0)
            matched = ms > lower and (upper == 0 or ms <= upper)
            reason = f"duration floor(ms)={ms}; require >{lower}" + (f" and <={upper}" if upper else '')
        elif self.kind == 'string_attribute':
            key, values = self.params['key'], self.params['values']
            found = sorted(trace.string_attributes.get(key, set()) & set(values))
            matched, reason = bool(found), f"exact resource/span matches for {key!r}: {found}"
        elif self.kind == 'not':
            matched = not children[0]['matched']
            reason = 'opposite of single child match (not a drop veto)'
        else:
            matched = all(c['matched'] for c in children)
            reason = 'all children must match' + ('; explicit drop veto' if self.kind == 'drop' else '')
        decision = ('dropped' if self.kind == 'drop' else 'sampled') if matched else 'not_sampled'
        return {'name': self.name, 'type': self.kind, 'matched': matched,
                'decision': decision, 'reason': reason, 'children': children}


def parse_policy(value, path, allowed):
    obj(value, path, {'name', 'type'} | LEAVES | {'and', 'not', 'drop'}, ('name', 'type'))
    name = string(value['name'], f'{path}.name', True)
    kind = string(value['type'], f'{path}.type', True)
    if kind not in allowed:
        raise InputError(f"{path}.type: unsupported policy {kind!r} at this nesting level")
    obj(value, path, {'name', 'type', kind})
    if kind == 'always_sample':
        obj(value.get(kind, {}), f'{path}.{kind}', set())
        return Policy(name, kind, {})
    if kind not in value:
        raise InputError(f'{path}: missing {kind} configuration')
    params = value[kind]
    p = f'{path}.{kind}'
    children = ()
    if kind == 'status_code':
        obj(params, p, {'status_codes'}, ('status_codes',))
        codes = array(params['status_codes'], f'{p}.status_codes', True)
        if any(not isinstance(c, str) or c not in {'OK', 'ERROR', 'UNSET'} for c in codes):
            raise InputError(f'{p}.status_codes: only OK, ERROR, UNSET are supported')
    elif kind == 'latency':
        obj(params, p, {'threshold_ms', 'upper_threshold_ms'}, ('threshold_ms',))
        for key, val in params.items():
            integer(val, f'{p}.{key}', maximum=2**63 - 1)
        upper = params.get('upper_threshold_ms', 0)
        if upper and upper <= params['threshold_ms']:
            raise InputError(f'{p}: nonzero upper_threshold_ms must exceed threshold_ms')
    elif kind == 'string_attribute':
        obj(params, p, {'key', 'values'}, ('key', 'values'))
        string(params['key'], f'{p}.key', True)
        for i, v in enumerate(array(params['values'], f'{p}.values', True)):
            string(v, f'{p}.values[{i}]')
    else:
        child_key = {'and': 'and_sub_policy', 'not': 'not_sub_policy', 'drop': 'drop_sub_policy'}[kind]
        obj(params, p, {child_key}, (child_key,))
        entries = [params[child_key]] if kind == 'not' else array(params[child_key], f'{p}.{child_key}', True)
        child_allowed = LEAVES if kind == 'not' else LEAVES | {'not'}
        children = tuple(parse_policy(v, f'{p}.{child_key}[{i}]', child_allowed) for i, v in enumerate(entries))
        unique_names(children, p)
    return Policy(name, kind, params, children)


def unique_names(policies, path):
    names = [p.name for p in policies]
    if len(names) != len(set(names)):
        raise InputError(f'{path}: duplicate sibling policy names')


def parse_config(text):
    try:
        value = yaml.load(text, Loader=StrictLoader)
        obj(value, 'config', {'processors'}, ('processors',))
        processors = value['processors']
        obj(processors, 'processors', {'tail_sampling'}, ('tail_sampling',))
        config = processors['tail_sampling']
        obj(config, 'processors.tail_sampling', {'policies'}, ('policies',))
        entries = array(config['policies'], 'processors.tail_sampling.policies', True)
        result = tuple(parse_policy(v, f'policies[{i}]', LEAVES | {'and', 'not', 'drop'}) for i, v in enumerate(entries))
        unique_names(result, 'policies')
        return result
    except (yaml.YAMLError, RecursionError) as exc:
        raise InputError(f'invalid YAML: {exc}') from exc


def load_config(path):
    return parse_config(Path(path).read_text(encoding='utf-8'))
