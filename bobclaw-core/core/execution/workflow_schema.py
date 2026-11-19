"""Strict workflow/v1 data validation. No provider or execution side effects."""
from copy import deepcopy
import json
import re

from .models import ExecutionError, canonical, integer, only

SCHEMA = 'workflow/v1'
ALIAS = re.compile(r'[a-z][a-z0-9_-]{0,47}\Z')
KINDS = {'agent', 'repair', 'foreach', 'check', 'gate', 'report'}


def fail(detail):
    raise ExecutionError('invalid_definition', 422, detail)


def obj(value, required, optional=()):
    only(value, set(required) | set(optional))
    if set(required) - value.keys():
        fail('missing fields: ' + ','.join(sorted(set(required) - value.keys())))


def string(value, low=0, high=30000):
    if (not isinstance(value, str) or not low <= len(value) <= high
            or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
        fail('invalid string')
    return value


def alias(value):
    if not isinstance(value, str) or not ALIAS.fullmatch(value):
        fail('invalid alias')
    return value


def array(value, maximum, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        fail('invalid array size')
    return value


def unique(value, maximum, minimum=0):
    array(value, maximum, minimum)
    if any(not isinstance(x, str) for x in value) or len(set(value)) != len(value):
        fail('duplicate or invalid identifier')
    return value


def skills(value):
    for s in array(value, 16):
        obj(s, {'skill_id', 'version', 'sha256'})
        string(s['skill_id'], 0, 262144)
        if type(s['version']) is not int or s['version'] != 1:
            fail('invalid skill version')
        if s['sha256'] is not None and not re.fullmatch('[0-9a-f]{64}', str(s['sha256'])):
            fail('invalid skill digest')


def validate(value):
    try:
        return _validate(value)
    except (TypeError, ValueError, KeyError, RecursionError):
        fail('malformed workflow data')


def _validate(value):
    try:
        size = len(canonical(value).encode('utf-8'))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        fail('definition must be JSON data')
    if size > 262144:
        fail('definition exceeds 256KiB')
    d = deepcopy(value)
    obj(d, {'schema', 'name', 'summary', 'agents', 'inputs', 'workspace', 'authority',
            'budget', 'review_policy', 'stages'}, {'distinct_models', 'extensions'})
    if d['schema'] != SCHEMA or d.get('extensions', {}) != {}:
        fail('unsupported schema/extensions')
    string(d['name'], 1, 80); string(d['summary'], 0, 1000)
    for key in ('agents', 'inputs'):
        if not isinstance(d[key], dict) or len(d[key]) > 16:
            fail('invalid ' + key)
        for name in d[key]:
            alias(name)
    for a in d['agents'].values():
        obj(a, {'face_id', 'binding_id'})
        string(a['face_id'], 1, 80)
        if a['binding_id'] is not None:
            string(a['binding_id'], 1, 128)
    for i in d['inputs'].values():
        obj(i, {'type', 'required'})
        if i['type'] not in {'string', 'evidence_ids'} or type(i['required']) is not bool:
            fail('invalid input declaration')
    obj(d['workspace'], {'required'})
    if type(d['workspace']['required']) is not bool:
        fail('invalid workspace')
    obj(d['authority'], {'workspace_ops', 'checks'})
    ops = unique(d['authority']['workspace_ops'], 4)
    if set(ops) - {'read', 'write', 'edit', 'check'} or set(ops) & {'write', 'edit', 'check'} and 'read' not in ops:
        fail('invalid workspace operations')
    checks = {}
    from core.builder.policy import workspace_path, task_check
    for c in array(d['authority']['checks'], 262144):
        obj(c, {'id', 'command', 'cwd', 'timeout_seconds'})
        alias(c['id']); string(c['command'], 1, 4096); string(c['cwd'], 0, 512)
        integer(c['timeout_seconds'], 1, 600)
        if c['id'] in checks or not workspace_path(c['cwd'], empty=True) or not task_check(c['command']):
            fail('duplicate or unsafe check')
        checks[c['id']] = c
    if checks and 'check' not in ops:
        fail('checks require check authority')
    obj(d['budget'], {'calls', 'active_seconds', 'job_seconds', 'max_job_attempts'})
    for k, maximum in [('calls', 100000), ('active_seconds', 604800), ('job_seconds', 604800), ('max_job_attempts', 10000)]:
        integer(d['budget'][k], 1, maximum)
    if d['review_policy'] not in {'any', 'different_model', 'different_family'}:
        fail('invalid review policy')
    for group in array(d.get('distinct_models', []), 16):
        unique(group, 16, 2)
        if set(group) - d['agents'].keys():
            fail('unknown distinct model alias')

    def step(s):
        obj(s, {'agent', 'brief', 'skills'})
        if s['agent'] not in d['agents']:
            fail('unknown agent')
        string(s['brief'], 1, 12000); skills(s['skills'])

    def block(stages, inside=False, prefix=''):
        array(stages, 16 if inside else 32, 1)
        ids = [alias(s.get('id')) if isinstance(s, dict) else fail('invalid stage') for s in stages]
        if len(set(ids)) != len(ids):
            fail('duplicate sibling id')
        by_id = dict(zip(ids, stages))
        closure, visiting, order = {}, set(), []

        def visit(key):
            if key in visiting:
                fail('dependency cycle')
            if key in closure:
                return closure[key]
            visiting.add(key)
            deps = unique(by_id[key].get('depends_on'), 32)
            if set(deps) - by_id.keys():
                fail('unknown dependency')
            found = set(deps)
            for dep in sorted(deps):
                found |= visit(dep)
            visiting.remove(key); closure[key] = found; order.append(key)
            return found
        for key in sorted(ids):
            visit(key)

        def ref(r, key):
            if not isinstance(r, dict):
                fail('invalid reference')
            if set(r) == {'input'}:
                if r['input'] not in d['inputs']:
                    fail('unknown input')
                return d['inputs'][r['input']]['type']
            if set(r) == {'item'} and r['item'] is True and inside:
                return 'item'
            if set(r) == {'literal'}:
                if len(canonical(r['literal']).encode()) > 8192:
                    fail('literal too large')
                return 'literal'
            if set(r) != {'stage', 'output'} or r['stage'] not in closure[key]:
                fail('reference producer must be a transitive sibling dependency')
            producer = by_id[r['stage']]
            outputs = {'agent': {'text', 'snapshot', producer.get('output')}, 'repair': {'text', 'verdict', 'snapshot'},
                       'foreach': {'items', 'snapshot'}, 'check': {'report'}, 'gate': set(), 'report': {'report'}}
            if r['output'] not in outputs.get(producer.get('kind'), set()):
                fail('invalid output reference')
            return r['output']

        static = expanded = 0
        reports = []
        for key in order:
            s = by_id[key]; kind = s.get('kind')
            fields = {'agent': {'agent', 'access', 'brief', 'output', 'skills'},
                      'repair': {'build', 'review', 'fix', 'check_ids', 'max_fixes'},
                      'foreach': {'source', 'max_units', 'width', 'body'}, 'check': {'check_ids', 'snapshot'},
                      'gate': {'prompt'}, 'report': set()}
            if kind not in KINDS or inside and kind in {'foreach', 'report'}:
                fail('invalid stage kind')
            obj(s, {'id', 'kind', 'depends_on'} | fields[kind], {'inputs'} | ({'review_of'} if kind == 'agent' else set()))
            if len(prefix + key) > 256:
                fail('expanded key too long')
            if not isinstance(s.get('inputs', {}), dict):
                fail('invalid stage inputs')
            for name, r in s.get('inputs', {}).items():
                alias(name); ref(r, key)
            count = worst = 1
            if kind == 'agent':
                step({k: s[k] for k in ('agent', 'brief', 'skills')})
                if s['access'] not in {'read', 'write'} or s['output'] not in {'text', 'units', 'verdict'}:
                    fail('invalid agent access/output')
                if s['access'] == 'write' and (not d['workspace']['required'] or d['agents'][s['agent']]['face_id'] != 'builder' or not set(ops) & {'write', 'edit'}):
                    fail('write requires Builder and workspace authority')
                review = s.get('review_of')
                if s['output'] == 'verdict' and not review:
                    fail('verdict requires review_of')
                if review:
                    if review not in closure[key] or by_id[review]['kind'] != 'agent' or s['access'] != 'read':
                        fail('invalid review edge')
                    if {'stage': review, 'output': 'snapshot'} not in s.get('inputs', {}).values():
                        fail('review must consume producer snapshot')
            elif kind in {'repair', 'check'}:
                for cid in unique(s['check_ids'], 262144, 1 if kind == 'check' else 0):
                    if cid not in checks:
                        fail('unknown check')
                if kind == 'repair':
                    for k in ('build', 'review', 'fix'):
                        step(s[k])
                    if not d['workspace']['required'] or not set(ops) & {'write', 'edit'} or any(d['agents'][s[k]['agent']]['face_id'] != 'builder' for k in ('build', 'fix')):
                        fail('repair requires Builder/workspace authority')
                    integer(s['max_fixes'], 0, 10)
                    count = 3 + bool(s['check_ids'])
                    worst = (2 + bool(s['check_ids'])) * (1 + s['max_fixes'])
                elif ref(s['snapshot'], key) != 'snapshot':
                    fail('check requires snapshot')
            elif kind == 'foreach':
                if ref(s['source'], key) != 'units':
                    fail('foreach requires units source')
                integer(s['max_units'], 1, 64); integer(s['width'], 1, 16)
                count, worst = block(s['body'], True, prefix + key + '/' + 'x' * 48 + '/')
                worst *= s['max_units']
            elif kind == 'gate':
                string(s['prompt'], 1, 1000)
            elif kind == 'report':
                reports.append(key)
            static += count; expanded += worst
        if not inside:
            if len(reports) > 1 or reports and (stages[-1]['id'] != reports[0] or closure[reports[0]] != set(ids) - set(reports)):
                fail('report must be final and depend on all stages')
            if len(stages) > 1 and not reports:
                fail('multi-stage definition needs report')
            gates = [s['id'] for s in stages if s['kind'] == 'gate']
            def writes(s):
                return s['kind'] == 'repair' or s['kind'] == 'agent' and s['access'] == 'write' or s['kind'] == 'foreach' and any(writes(x) for x in s['body'])
            def internally_gated(s):
                if s['kind']!='foreach': return False
                body={x['id']:x for x in s['body']}; cache={}
                def ancestors(key):
                    if key not in cache:
                        cache[key]=set(body[key]['depends_on']).union(*(ancestors(x) for x in body[key]['depends_on']))
                    return cache[key]
                gate_deps=[ancestors(x['id']) for x in s['body'] if x['kind']=='gate']
                return all(not writes(x) or any(x['id'] in deps for deps in gate_deps) for x in s['body'])
            for s in stages:
                if len(stages) > 1 and writes(s) and not internally_gated(s) and not any(s['id'] in closure[g] for g in gates):
                    fail('write path must reach owner gate')
        return static, expanded

    static, expanded = block(d['stages'])
    if static > 128 or expanded > 4096:
        fail('worst-case expansion exceeds bounds')
    return d


def bounds(d):
    def count(stages):
        total = 0
        for s in stages:
            total += (s['max_units'] * count(s['body']) if s['kind'] == 'foreach' else
                      (2 + bool(s['check_ids'])) * (1 + s['max_fixes']) if s['kind'] == 'repair' else 1)
        return total
    return count(d['stages'])


def stages(d):
    """Deterministic sibling topological order (ID breaks ready ties)."""
    def walk(items, prefix=''):
        remaining = {s['id']: s for s in items}; done = set()
        while remaining:
            key = min(k for k, s in remaining.items() if set(s['depends_on']) <= done)
            s = remaining.pop(key); done.add(key)
            yield prefix + key, s
            if s['kind'] == 'foreach':
                yield from walk(s['body'], prefix + key + '/*/')
    return list(walk(d['stages']))


def validate_inputs(d, values):
    if not isinstance(values,dict):
        raise ExecutionError('invalid_args',422,'inputs: expected an object')
    for key in values.keys()-d['inputs'].keys():
        raise ExecutionError('unsupported_fields',422,'inputs.'+str(key)+': expected a declared workflow input')
    for key, spec in d['inputs'].items():
        if key not in values:
            if spec['required']:
                raise ExecutionError('missing_input',422,'inputs.'+key+': expected the required '+spec['type']+' input')
            continue
        try:
            if spec['type'] == 'string':
                string(values[key], 0, 30000)
            else:
                for item in array(values[key], 64):
                    string(item, 1, 256)
        except ExecutionError as exc:
            expected='a string of at most 30000 characters' if spec['type']=='string' else 'an array of at most 64 evidence IDs, each 1 to 256 characters'
            raise ExecutionError(exc.code,422,'inputs.'+key+': expected '+expected) from None
    return deepcopy(values)


def validate_units(value, max_units):
    from .workflow_output import validate_output
    return validate_output('units', value, max_units=max_units)


def validate_verdict(value, snapshot_digest=None):
    from .workflow_output import validate_output
    return validate_output('verdict', value, snapshot_digest=snapshot_digest)

def proposal_args(a):
    """Actionable field diagnostics; authorization is checked by the service."""
    def require(field, expected, condition):
        if not condition:
            raise ExecutionError('invalid_args',422,field+': expected '+expected)
    for field in ('inputs','bindings','expires_at','request_key'):
        require(field,'a required proposal field',field in a)
    require('definition_ref / definition','exactly one definition_ref object or inline definition',
            ('definition_ref' in a) != ('definition' in a))
    for field in ('inputs','bindings'):
        require(field,'an object',isinstance(a[field],dict))
    require('expires_at','a UTC ISO-8601 string ending in Z or +00:00',isinstance(a['expires_at'],str))
    require('request_key','a nonempty string of at most 128 characters',
            isinstance(a['request_key'],str) and bool(a['request_key'].strip()) and 1<=len(a['request_key'])<=128)
    require('run_mode','auto or approve',isinstance(a.get('run_mode','auto'),str)
            and a.get('run_mode','auto') in {'auto','approve'})
    if 'definition_ref' in a:
        ref=a['definition_ref']
        require('definition_ref','an object with id and revision',isinstance(ref,dict))
        for field in ('id','revision'):
            require('definition_ref.'+field,'a required field',field in ref)
        for field in ref.keys()-{'id','revision'}:
            require('definition_ref.'+field,'no extra field; use id and revision',False)
        require('definition_ref.id','a workflow identifier of 1 to 128 letters, digits, underscore, dot, colon or hyphen',
                isinstance(ref['id'],str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',ref['id'])))
        revision=ref['revision']
        require('definition_ref.revision','a positive integer (or decimal integer string)',
                type(revision) is int and 1<=revision<=9223372036854775807 or isinstance(revision,str)
                and bool(re.fullmatch('[1-9][0-9]{0,18}',revision)) and int(revision)<=9223372036854775807)
    else:
        require('definition','an inline workflow/v1 object',isinstance(a['definition'],dict))
    if a.get('workspace') is not None:
        workspace=a['workspace']
        require('workspace','an object with create, e.g. {"create":"Hello workspace"}',isinstance(workspace,dict))
        require('workspace.create','a required workspace name', 'create' in workspace)
        for field in workspace.keys()-{'create'}:
            require('workspace.'+field,'no extra field; use create',False)
        name=workspace['create']
        require('workspace.create','a nonblank name of 1 to 200 characters without control characters',
                isinstance(name,str) and 1<=len(name)<=200 and bool(name.strip()) and not any(ord(c)<32 or 0xD800<=ord(c)<=0xDFFF for c in name))
        require('workspace / project_id','either a new workspace or an existing project_id, never both',not a.get('project_id'))
