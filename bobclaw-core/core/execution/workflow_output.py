"""Shared definitions for typed stage instructions and strict answer validation.

Only model answer objects ignore extras. Grant and workflow schemas stay strict.
Normalization never supplies a required key or translates a verdict.
"""
from copy import deepcopy
import json
import re

from .models import ExecutionError, canonical

ID_PATTERN = r'[a-z][a-z0-9_-]{0,47}'
DIGEST_PATTERN = r'[0-9a-f]{64}'
UNIT_DEPTH = 16
VERDICTS = {'pass': 'the reviewed work meets the supplied criteria',
            'changes': 'the work needs the listed fixes',
            'blocked': 'an owner answer is needed before judging the work'}


def text(low, high, **kw):
    return {'type': 'string', 'min': low, 'max': high, **kw}


def object_(fields):
    return {'type': 'object', 'fields': fields}


def array_(item, low=0, high=None, **kw):
    return {'type': 'array', 'item': item, 'min': low, 'max': high, **kw}


IDENTIFIER = text(1, 48, pattern=ID_PATTERN)
VERDICT = object_({
    'verdict': text(1, 7, enum=VERDICTS),
    'summary': text(0, 4000),
    'findings': array_(object_({'id': IDENTIFIER, 'detail': text(0, 2000),
        'evidence_ids': array_(text(1, 256), high=64)}), unique_by='id'),
    'question': {'type': 'question', 'min': 1, 'max': 30000},
    'snapshot_digest': text(64, 64, pattern=DIGEST_PATTERN),
})
UNIT = object_({'id': IDENTIFIER, 'title': text(1, 120), 'brief': text(1, 12000),
    'depends_on': array_(text(0, 30000), high=64, unique=True),
    'acceptance': text(0, 4000, optional=True)})


def contract(kind, max_units=64):
    if kind == 'verdict':
        return VERDICT
    if kind == 'units' and type(max_units) is int and 1 <= max_units <= 64:
        return object_({'units': array_(UNIT, 1, max_units, unique_by='id', depth=UNIT_DEPTH)})
    raise ValueError('unknown typed output or invalid units limit')


def invalid(detail):
    raise ExecutionError('invalid_definition', 422, detail)


def check(rule, value, key='answer', parent=None):
    kind = rule['type']
    if kind == 'object':
        if not isinstance(value, dict):
            invalid(f"'{key}' must be a JSON object")
        result = {}
        for field, spec in rule['fields'].items():
            if field not in value:
                if not spec.get('optional'):
                    missing = field if key == 'answer' else key+'.'+field
                    invalid(f"the key '{missing}' is missing")
                continue
            result[field] = check(spec, value[field], field if key == 'answer' else key+'.'+field, value)
        return result
    if kind == 'question':
        if parent['verdict'] != 'blocked':
            if value is not None:
                invalid("'question' must be null unless 'verdict' is 'blocked'")
            return None
        if not isinstance(value, str) or not value.strip():
            invalid("a blocked review needs a nonblank 'question'")
        return check(text(rule['min'], rule['max']), value, key)
    if kind == 'string':
        if not isinstance(value, str):
            invalid(f"'{key}' must be a string")
        if 'enum' in rule and value not in rule['enum']:
            invalid(f"'{key}' must be one of "+', '.join(rule['enum']))
        if not rule['min'] <= len(value) <= rule['max'] or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            invalid(f"'{key}' must contain {rule['min']} to {rule['max']} characters without unpaired surrogates")
        if 'pattern' in rule and not re.fullmatch(rule['pattern'], value):
            invalid(f"'{key}' must match {rule['pattern']}")
        return value
    if not isinstance(value, list):
        invalid(f"'{key}' must be an array")
    if len(value) < rule['min'] or rule['max'] is not None and len(value) > rule['max']:
        invalid(f"'{key}' must have {rule['min']} to {rule['max']} items")
    result = [check(rule['item'], item, f'{key}[{i}]') for i, item in enumerate(value)]
    identities = [x[rule['unique_by']] for x in result] if rule.get('unique_by') else result
    if (rule.get('unique') or rule.get('unique_by')) and len(set(identities)) != len(identities):
        invalid(f"'{key}' contains duplicate "+(rule.get('unique_by') or 'values'))
    if rule.get('depth'):
        by_id = {u['id']: u for u in result}
        visiting, depths = set(), {}
        def visit(identifier):
            if identifier not in by_id:
                invalid(f"'{key}' has unknown dependency '{identifier}'")
            if identifier in visiting:
                invalid(f"'{key}' has a dependency cycle at '{identifier}'")
            if identifier not in depths:
                visiting.add(identifier)
                depth = 1 + max((visit(dep) for dep in by_id[identifier]['depends_on']), default=0)
                if depth > rule['depth']:
                    invalid(f"'{key}' dependency depth exceeds {rule['depth']}")
                visiting.remove(identifier)
                depths[identifier] = depth
            return depths[identifier]
        for identifier in by_id:
            visit(identifier)
    return result


def validate_output(kind, value, *, snapshot_digest=None, max_units=64):
    try:
        result = check(contract(kind, max_units), value)
    except (TypeError, ValueError, KeyError, RecursionError):
        invalid(f'the {kind} answer is malformed')
    if kind == 'verdict' and snapshot_digest is not None and result['snapshot_digest'] != snapshot_digest:
        raise ExecutionError('review_snapshot_mismatch', 409,
            'the snapshot digest does not match; use '+snapshot_digest)
    return deepcopy(result)


def read_object(answer):
    """Read one JSON container amid prose/fences, never choose between two.

    Decode whole containers so nested objects cannot become top-level answers.
    Duplicate object keys and non-JSON numbers fail closed.
    """
    if not isinstance(answer, str):
        invalid('the answer must be text containing one JSON object')
    if len(answer) > 30000:
        invalid('the answer exceeds 30000 characters')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                invalid(f"the JSON key '{key}' occurs more than once")
            result[key] = value
        return result
    def constant(value):
        invalid('the answer contains a non-JSON number')
    decoder = json.JSONDecoder(object_pairs_hook=pairs, parse_constant=constant)
    containers = []
    offset = 0
    while offset < len(answer):
        if answer[offset] not in '{[':
            offset += 1
            continue
        try:
            value, end = decoder.raw_decode(answer, offset)
        except (ValueError, RecursionError):
            invalid('the JSON object could not be read; return one complete JSON object')
        containers.append(value)
        offset = end
    if len(containers) != 1 or not isinstance(containers[0], dict):
        invalid('return exactly one top-level JSON object; '+
                ('multiple JSON containers were found' if len(containers) > 1 else 'no single JSON object was found'))
    return containers[0]


def describe(rule, key='answer'):
    kind = rule['type']
    if kind == 'object':
        fields = rule['fields']
        lines = [f"'{key}': object; required keys: "+', '.join(k for k,v in fields.items() if not v.get('optional'))+'.']
        for field, spec in fields.items():
            lines.extend(describe(spec, field if key == 'answer' else key+'.'+field))
        return lines
    optional = ' (optional)' if rule.get('optional') else ''
    if kind == 'question':
        return [f"'{key}': null unless verdict is blocked; when blocked, a nonblank string of {rule['min']} to {rule['max']} characters asking the owner what is needed."]
    if kind == 'string':
        line = f"'{key}'{optional}: string, {rule['min']} to {rule['max']} characters, without unpaired surrogates"
        if 'pattern' in rule:
            line += '; must match '+rule['pattern']
        if 'enum' in rule:
            line += '; allowed values: '+'; '.join(f'{k} = {v}' for k,v in rule['enum'].items())
        return [line+'.']
    maximum = rule['max'] if rule['max'] is not None else 'any number of'
    line = f"'{key}': array, {rule['min']} to {maximum} items"
    if rule.get('unique_by'):
        line += '; unique '+rule['unique_by']
    if rule.get('unique'):
        line += '; unique values'
    if rule.get('depth'):
        line += f"; dependencies must reference these unit IDs, have no cycles and have depth at most {rule['depth']}"
    return [line+'.', *describe(rule['item'], key+'[]')]


def output_example(kind, snapshot_digest=None):
    if kind == 'verdict':
        return {'verdict':'changes','summary':'One criterion needs a fix.',
            'findings':[{'id':'criterion-one','detail':'Describe the observed issue and cite supplied evidence IDs when available.', 'evidence_ids':[]}],
            'question':None, 'snapshot_digest':snapshot_digest}
    return {'units':[{'id':'hello','title':'Create greeting','brief':'Create hello.txt containing hello.',
                      'depends_on':[], 'acceptance':'hello.txt contains exactly hello.'}]}


def output_instructions(kind, *, snapshot_digest=None, max_units=64):
    rule = contract(kind, max_units)
    example = output_example(kind, snapshot_digest)
    validate_output(kind, example, snapshot_digest=snapshot_digest, max_units=max_units)
    extra = ('Evidence IDs come from the supplied input evidence and tool receipts; copy real IDs, never invent them. '
             'If no supplied evidence ID applies, use an empty evidence_ids array and describe the observation in detail. '
             'Return this exact snapshot_digest: '+str(snapshot_digest)+'.') if kind == 'verdict' else ''
    return '\n'.join(['Required '+kind+' answer format. Reply with only one JSON object, without prose or fences.',
        'Unknown extra keys are ignored; every required key and value remains strict.',
        *describe(rule), extra, 'Filled-in example (use your actual judgment and evidence):',
        canonical(example)])
