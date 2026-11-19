"""Negotiated checked-work data contracts. No model-selected executable authority."""
from __future__ import annotations

import copy
import hashlib
import re

from .models import ExecutionError, canonical, digest, integer, only, text
from .planning import identifier, strict_json
from core.portal_resources import relative_parts, ResourceError

CONTRACT = 'checked-edits/1'
PROFILE = 'checked'
KINDS = ('model_task', 'workspace_read', 'workspace_edit', 'worker_command',
         'registered_check', 'proposal_publish', 'await_owner_decision', 'integrate_approved_proposal')
# Definitions are installed code, never supplied by a planner or a start request.
CHECKS = {
    'python.compile': {'revision': 1, 'adapter': 'python.compile', 'effect': 'workspace_mutation',
                       'arguments': ['-B', '-c', 'import pathlib,py_compile; files=list(pathlib.Path("app").rglob("*.py")); assert files,"no Python sources"; [py_compile.compile(str(p),doraise=True) for p in files]']},
    'python.unittest': {'revision': 1, 'adapter': 'python.unittest', 'effect': 'workspace_mutation',
                        'arguments': ['-B', '-m', 'unittest', 'discover', '-s', 'app', '-v']},
    'files.nonempty': {'revision': 1, 'adapter': 'files.nonempty', 'effect': 'workspace_mutation',
                       'assertion': 'at_least_one_nonempty_regular_file'},
    'python.import': {'revision': 1, 'adapter': 'python.import', 'effect': 'workspace_mutation',
                      'arguments': ['-B','-c','import minikit']},
    'python.collect': {'revision': 1, 'adapter': 'python.collect', 'effect': 'workspace_mutation',
                       'arguments': ['-B','-m','pytest','--collect-only','-q','-p','no:cacheprovider','tests']},
    'python.pytest': {'revision': 1, 'adapter': 'python.pytest', 'effect': 'workspace_mutation',
                      'arguments': ['-B','-m','pytest','-q','--no-header','-p','no:cacheprovider','tests']},
    'python.cli': {'revision': 1, 'adapter': 'python.cli', 'effect': 'workspace_mutation',
                   'arguments': ['-B','-m','minikit.cli']},
}
OPERATIONS = {'workspace.read': 'workspace_read', 'workspace.edit': 'workspace_edit',
              'python.scaffold': 'worker_command', 'python.baseline': 'worker_command', **{k: 'worker_command' for k in CHECKS}}
SHA = re.compile('[0-9a-f]{64}')


def protected(name):
    return (name == 'tests' or name.startswith('tests/') or
            any(p.startswith('test_') and p.endswith('.py') for p in name.split('/')) or
            any(p.casefold() in {'conftest.py','pytest.ini','pyproject.toml','setup.cfg','tox.ini'} for p in name.split('/')))


def path(value):
    try:
        parts = relative_parts(value)
    except ResourceError as exc:
        raise ExecutionError('invalid_edit_path', exc.status) from None
    # Eliminate aliases on Windows too; runtime enforcement uses Linux dirfds.
    if any(p.endswith((' ', '.')) or p.split('.')[0].upper() in
           {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
           for p in parts):
        raise ExecutionError('invalid_edit_path')
    return '/'.join(parts)


def edits(value):
    only(value, {'edits'})
    items = value.get('edits')
    if not isinstance(items, list) or not 1 <= len(items) <= 128:
        raise ExecutionError('invalid_edits')
    seen, total = set(), 0
    for item in items:
        only(item, {'operation', 'path', 'pre_sha256', 'content'})
        name = path(item.get('path'))
        if name.casefold() in seen:
            raise ExecutionError('duplicate_edit_path')
        seen.add(name.casefold())
        op = item.get('operation')
        pre = item.get('pre_sha256')
        if 'pre_sha256' not in item or op not in {'create', 'replace', 'delete'} or (pre is not None and (not isinstance(pre, str) or not SHA.fullmatch(pre))):
            raise ExecutionError('invalid_edits')
        if (op == 'create') != (pre is None):
            raise ExecutionError('expected_preimage_required')
        if op == 'delete':
            if 'content' in item:
                raise ExecutionError('invalid_edits')
        else:
            content = item.get('content')
            if not isinstance(content, str):
                raise ExecutionError('invalid_edits')
            try:
                total += len(content.encode('utf-8'))
            except UnicodeError:
                raise ExecutionError('invalid_edits') from None
    if total > 240000 or len(canonical(value).encode()) > 500000:
        raise ExecutionError('edits_too_large', 413)
    names = sorted(seen)
    if any(b.startswith(a + '/') for a in names for b in names if a != b):
        raise ExecutionError('overlapping_edit_paths')
    return value


def acceptance(value):
    only(value, {'criteria'})
    criteria = value.get('criteria')
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 100:
        raise ExecutionError('invalid_acceptance')
    seen = set()
    for item in criteria:
        only(item, {'id', 'required', 'output_kind', 'checks'})
        cid = identifier(item.get('id'))
        if cid in seen or type(item.get('required', True)) is not bool or item.get('output_kind') != 'proposal':
            raise ExecutionError('invalid_acceptance')
        seen.add(cid)
        only(item.get('checks'), {'required_check_ids'})
        ids = item['checks'].get('required_check_ids')
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in CHECKS for i in ids) or len(set(ids)) != len(ids):
            raise ExecutionError('registered_checks_required')
    if not any(c.get('required', True) for c in criteria):
        raise ExecutionError('required_acceptance_missing')
    return value


def allowed_edit(item, write_paths, allow_new_tests=False):
    name = path(item['path'])
    new_test = (allow_new_tests and item['operation'] == 'create' and item['pre_sha256'] is None
                and name.startswith('app/') and len(name.split('/')) == 2
                and name.split('/')[-1].startswith('test_') and name.endswith('.py'))
    return (new_test or not protected(name)) and any(name == p or name.startswith(p + '/') for p in write_paths)


def edit_scope(value, write_paths, allow_new_tests=False):
    edits(value)
    for item in value['edits']:
        if not allowed_edit(item, write_paths, allow_new_tests):
            raise ExecutionError('edit_scope_denied',403)
    return value


def pinned_recipe(value):
    only(value, {'id', 'revision', 'definition', 'digest'})
    from .models import slug
    slug(value.get('id'))
    integer(value.get('revision'))
    definition = value.get('definition')
    only(definition, {'id', 'revision', 'checks', 'steps', 'target'})
    ids = definition.get('checks')
    if (definition.get('id') != value['id'] or definition.get('revision') != value['revision']
            or value.get('digest') != digest(definition)):
        raise ExecutionError('recipe_digest_conflict', 409)
    if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in CHECKS for i in ids)
            or len(set(ids)) != len(ids)):
        raise ExecutionError('recipe_checks_unregistered', 409)
    return definition


def unittest_evidence(output):
    """Bounded unittest -v identities, including failing subtests' parent IDs."""
    output = output.replace('\r\n', '\n')
    pattern = r'(\S+ \([^\n()]+\))'
    passed = set(re.findall(r'^' + pattern + r'(?:\n[^\n]*)? \.\.\. ok$', output, re.M))
    failed = set(re.findall(r'^' + pattern + r'(?: .*?)? \.\.\. (?:FAIL|ERROR)\b', output, re.M))
    failed.update(re.findall(r'^(?:FAIL|ERROR): ' + pattern, output, re.M))
    counts = re.findall(r'^Ran (\d+) tests?\b', output, re.M)
    if len(counts) != 1 or int(counts[0]) < 1:
        raise ExecutionError('no_tests_executed')
    if len(passed | failed) > 512 or any(len(i) > 512 for i in passed | failed):
        raise ExecutionError('test_evidence_limit', 409)
    return {'tests_run': int(counts[0]), 'passed_test_ids': sorted(passed - failed),
            'failing_test_ids': sorted(failed)}


def compare_baseline(baseline, final):
    if (not isinstance(baseline, dict) or not isinstance(final, dict)
            or type(baseline.get('tests_run')) is not int or baseline['tests_run'] < 1):
        return 'baseline_receipt_unavailable'
    if type(final.get('tests_run')) is not int or final['tests_run'] < baseline['tests_run']:
        return 'baseline_tests_missing'
    if not isinstance(baseline.get('failing_test_ids'), list) or not isinstance(final.get('passed_test_ids'), list):
        return 'baseline_receipt_unavailable'
    if not baseline.get('passed') and not baseline['failing_test_ids']:
        return 'baseline_receipt_unavailable'
    if final.get('exit_status') != 0 or not set(baseline['failing_test_ids']) <= set(final['passed_test_ids']):
        return 'baseline_regression_not_repaired'
    return None


def registration(folder, frozen, profile_revision, direct_mode=None, work_recipe=None):
    config = folder.get('checked_edits')
    only(config, {'checks', 'write_paths', 'build_contracts'})
    ids, paths = config.get('checks'), config.get('write_paths')
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in CHECKS for i in ids) or len(set(ids)) != len(ids):
        raise ExecutionError('invalid_check_registration')
    if direct_mode and work_recipe is not None:
        selected = pinned_recipe(work_recipe)
        if not set(selected['checks']) <= set(ids):
            raise ExecutionError('recipe_checks_unregistered', 409)
        ids = selected['checks']
        if direct_mode == 'fixer' and 'python.unittest' not in ids:
            raise ExecutionError('fixer_unittest_required', 409)
    if not isinstance(paths, list) or not 1 <= len(paths) <= 32:
        raise ExecutionError('invalid_write_registration')
    paths = [path(p) for p in paths]
    if any(protected(p) for p in paths):
        raise ExecutionError('baseline_tests_protected')
    units = config.get('build_contracts')
    if units is not None:
        from core.build.contracts import coerce_units
        if not isinstance(units, list) or not 1 <= len(units) <= 128:
            raise ExecutionError('invalid_build_contracts')
        normalized = coerce_units(units, len(units))
        if len(normalized) != len(units):
            raise ExecutionError('invalid_build_contracts')
        units = copy.deepcopy(normalized)
    tests = [x for x in frozen['manifest'] if x['path'] == 'tests' or x['path'].startswith('tests/')]
    if any(i.startswith('python.') and i not in {'python.compile', 'python.unittest'} for i in ids) and not (units or any(x['kind'] == 'file' for x in tests)):
        raise ExecutionError('baseline_tests_required')
    definitions = {i: {**copy.deepcopy(CHECKS[i]), 'id': i, 'profile_revision': profile_revision,
                       'command_spec_digest': digest(CHECKS[i]), 'test_provenance': 'registered_contracts' if units else 'registered_base',
                       'tests_digest': digest(units if units else tests)} for i in ids}
    operations = {k:v for k,v in OPERATIONS.items() if v!='worker_command' or k in ids or k=='python.scaffold' and units}
    operations['edits.plan'] = 'model_task'
    if direct_mode == 'fixer': operations['python.baseline'] = 'worker_command'
    return {'checks': definitions, 'operations':operations, 'write_paths': paths, 'build_contracts': units,
            'profile_revision': profile_revision, 'base_digest': frozen['digest'],
            'direct_mode': direct_mode, 'allow_new_tests': direct_mode in {'builder', 'fixer'}}


def parse_plan(raw, contract, context):
    """Validate the whole DAG before persisting any executable step."""
    from .planning import parse_plan as text_plan
    try:
        value = strict_json(raw)
    except (ValueError, TypeError, RecursionError):
        raise ExecutionError('invalid_plan_schema') from None
    only(value, {'tasks'})
    tasks = value.get('tasks')
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 64:
        raise ExecutionError('invalid_plan_schema')
    criteria = {c['id']: c for c in contract['criteria']}
    shadow, by_id = [], {}
    for task in tasks:
        only(task, {'id', 'kind', 'instruction', 'dependencies', 'snapshot', 'operation_id', 'check_id',
                    'proposed_seconds', 'rationale', 'criteria_ids', 'paths', 'edits', 'edits_from'})
        key = identifier(task.get('id'))
        kind = task.get('kind')
        if not isinstance(kind, str) or kind not in KINDS:
            raise ExecutionError('unknown_step_kind')
        if kind in {'await_owner_decision', 'integrate_approved_proposal'}:
            raise ExecutionError('managed_apply_unavailable_until_4.3e', 409)
        common = {'id', 'kind', 'instruction', 'dependencies', 'snapshot', 'criteria_ids'}
        specific = {'model_task': {'operation_id'}, 'workspace_read': {'operation_id','paths','proposed_seconds','rationale'},
                    'workspace_edit': {'operation_id','edits','edits_from','proposed_seconds','rationale'},
                    'worker_command': {'operation_id','proposed_seconds','rationale'},
                    'registered_check': {'check_id','proposed_seconds','rationale'}, 'proposal_publish': set()}
        only(task, common | specific[kind])
        if key in by_id:
            raise ExecutionError('duplicate_task')
        by_id[key] = task
        text(task.get('instruction'), 10000)
        deps, cids = task.get('dependencies'), task.get('criteria_ids', [])
        if not isinstance(cids, list) or any(not isinstance(i, str) or i not in criteria for i in cids) or len(set(cids)) != len(cids):
            raise ExecutionError('invalid_criterion_assignment')
        if kind != 'proposal_publish' and cids:
            raise ExecutionError('unchecked_output_not_deliverable')
        if kind == 'model_task':
            if task.get('operation_id') != 'edits.plan':
                raise ExecutionError('unregistered_operation')
        elif kind in {'workspace_read', 'workspace_edit', 'worker_command'}:
            operation = task.get('operation_id')
            if not isinstance(operation, str) or OPERATIONS.get(operation) != kind:
                raise ExecutionError('unregistered_operation')
            if kind == 'worker_command' and operation != 'python.scaffold' and operation not in context['checks'] and not (operation == 'python.baseline' and context.get('direct_mode') == 'fixer'):
                raise ExecutionError('unregistered_operation')
            if operation == 'python.scaffold' and not context.get('build_contracts'):
                raise ExecutionError('registered_build_contracts_required')
        elif kind == 'registered_check' and (not isinstance(task.get('check_id'), str) or task.get('check_id') not in context['checks']):
            raise ExecutionError('unregistered_check')
        if kind not in {'model_task', 'proposal_publish'}:
            if task.get('proposed_seconds') not in (900, 1800, 3600) or type(task.get('proposed_seconds')) is not int:
                raise ExecutionError('invalid_proposed_tier')
            text(task.get('rationale'), 2000)
        snapshot_id = task.get('snapshot')
        if not isinstance(snapshot_id, str) or (snapshot_id != context['base_digest'] and snapshot_id not in (deps or [])):
            raise ExecutionError('invalid_snapshot_reference')
        if kind == 'workspace_read':
            paths = task.get('paths')
            if not isinstance(paths, list) or not 1 <= len(paths) <= 32:
                raise ExecutionError('invalid_read_paths')
            for p in paths:
                path(p)
        if kind == 'workspace_edit':
            if ('edits' in task) == ('edits_from' in task):
                raise ExecutionError('edit_source_required')
            if 'edits' in task:
                edit_scope({'edits': task['edits']},context['write_paths'], context.get('allow_new_tests', False))
            elif not isinstance(task['edits_from'], str) or task['edits_from'] not in (deps or []):
                raise ExecutionError('edit_source_dependency_required')
        shadow.append({'id': key, 'instruction': task['instruction'], 'dependencies': deps,
                       'output_kind': 'intermediate', 'criteria_ids': [], 'checks': {}})
    # Use the same cycle, width and antichain guard as text plans. Typed steps
    # need room for read -> model -> edit -> four checks -> publication.
    text_plan(canonical({'tasks': shadow}), {'criteria': []}, max_depth=16)
    publishes = [t for t in tasks if t['kind'] == 'proposal_publish']
    if len(publishes) != 1 or not {c['id'] for c in contract['criteria'] if c.get('required',True)} <= set(publishes[0].get('criteria_ids', [])):
        raise ExecutionError('single_complete_publication_required')
    for task in tasks:
        if task.get('edits_from') and by_id[task['edits_from']]['kind'] != 'model_task':
            raise ExecutionError('invalid_edit_source')
        if task['kind'] == 'proposal_publish':
            required = set(context['checks']) | {i for cid in task.get('criteria_ids', []) for i in criteria[cid]['checks']['required_check_ids']}
            if not required or not required <= set(context['checks']):
                raise ExecutionError('required_check_missing')
            ancestors = set(task['dependencies'])
            pending = list(ancestors)
            while pending:
                for dep in by_id[pending.pop()]['dependencies']:
                    if dep not in ancestors:
                        ancestors.add(dep)
                        pending.append(dep)
            planned = {by_id[d]['check_id'] for d in ancestors if by_id[d]['kind'] == 'registered_check'}
            if not required <= planned:
                raise ExecutionError('required_check_missing')
    return value


def direct_plan(contract, context):
    """Installed single-model recipe. Planning consumes no pretend model call."""
    base = context['base_digest']
    tasks = []
    if context.get('direct_mode') == 'fixer':
        tasks.append({'id':'baseline','kind':'worker_command','instruction':'Record the existing unittest baseline, including failures.',
                      'dependencies':[],'snapshot':base,'operation_id':'python.baseline',
                      'proposed_seconds':900,'rationale':'Reproduce before editing'})
    tasks.extend([
        {'id':'edit-plan','kind':'model_task','operation_id':'edits.plan',
         'instruction':'Implement the requested focused Python change and regression tests inside app/. Preserve existing tests.',
         'dependencies':['baseline'] if tasks else [],'snapshot':'baseline' if tasks else base},
        {'id':'edit','kind':'workspace_edit','operation_id':'workspace.edit','edits_from':'edit-plan',
         'instruction':'Apply exact declared edits to the frozen work copy.',
         'dependencies':['edit-plan'],'snapshot':'edit-plan','proposed_seconds':900,'rationale':'Bounded recipe edit'}])
    for index, cid in enumerate(context['checks']):
        tasks.append({'id':'check-'+str(index),'kind':'registered_check','check_id':cid,
                      'instruction':'Run the installed '+cid+' check on the edited snapshot.',
                      'dependencies':['edit'],'snapshot':'edit','proposed_seconds':900,'rationale':'Verify edited bytes'})
    tasks.append({'id':'proposal','kind':'proposal_publish','instruction':'Publish a checked proposal, without changing managed source.',
                  'dependencies':['edit',*[t['id'] for t in tasks if t['kind']=='registered_check']],
                  'snapshot':'edit','criteria_ids':[c['id'] for c in contract['criteria']]})
    return parse_plan(canonical({'tasks':tasks}),contract,context)


def accepted_manifest(*, run_id, step, proposal, receipts, contract, context):
    """A published, digest-bound deliverable; logs and scratch never count."""
    required = set(context['checks']) | {i for c in contract['criteria'] if c['id'] in step['criteria_ids']
                for i in c['checks']['required_check_ids']}
    valid = {}
    for receipt in receipts:
        cid = receipt['check_id']
        definition = context['checks'].get(cid)
        if (definition and receipt.get('run_id') == run_id and receipt['definition_digest'] == digest(definition)
                and receipt['input_digest'] == proposal['output_digest']
                and receipt['passed'] is True and receipt['cleanup'] == 'idle_proved'
                and receipt.get('source_unchanged') is True
                and not receipt.get('truncated') and receipt['outcome'] == 'succeeded'):
            valid[cid] = receipt
    changed = any(f.get('operation') == 'delete' and f.get('pre_sha256') or
                  f.get('bytes',0)>0 and f.get('post_sha256') != f.get('pre_sha256')
                  for f in proposal['files'])
    if not required or not required <= valid.keys() or not changed:
        raise ExecutionError('checked_output_required', 409)
    manifest = {'schema_version': 1, 'kind': 'checked_proposal', 'run_id': run_id,
                'step_lineage': [step['plan_step'], step['ordinal']], 'criteria_ids': step['criteria_ids'],
                'proposal_digest': proposal['digest'], 'base_digest': proposal['base_digest'],
                'output_digest': proposal['output_digest'], 'checks': [valid[i] for i in sorted(required)],
                'independently_verified': False}
    return {**manifest, 'digest': digest(manifest)}


def check_source_unchanged(base, scratch):
    before = {i['path']:i for i in base['manifest']}
    after = {i['path']:i for i in scratch['manifest']}
    return (all(after.get(name)==item for name,item in before.items()) and
            not any(protected(name) for name in after.keys()-before.keys()))
