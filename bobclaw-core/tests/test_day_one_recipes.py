"""Focused recipe, Fixer evidence, configured binding and face regressions."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.execution.checked import (compare_baseline, direct_plan, pinned_recipe,
                                    registration, unittest_evidence)
from core.execution.models import ExecutionError, digest, start_request
from core.execution.workbench import DEFAULT, Workbench, configured_binding
from core.execution.workspaces import snapshot
from core.tool_jobs.checked_runner import execute


def pin(checks=('python.compile',)):
    definition = {**DEFAULT, 'id': 'compile-only', 'checks': list(checks)}
    return {'id': definition['id'], 'revision': 1, 'definition': definition, 'digest': digest(definition)}


def test_compile_recipe_selects_exactly_one_executable_check_without_tests(tmp_path, capfd):
    app = tmp_path / 'app'; app.mkdir(); (app / 'code.py').write_text('answer = 42\n')
    frozen = snapshot(tmp_path)
    folder = {'checked_edits': {'checks': ['python.compile', 'python.unittest'], 'write_paths': ['app']}}
    ctx = registration(folder, frozen, 'rev', 'builder', pin())
    contract = {'criteria': [{'id': 'output', 'output_kind': 'proposal',
                              'checks': {'required_check_ids': ['python.compile']}}]}
    tasks = direct_plan(contract, ctx)['tasks']
    checks = [t['check_id'] for t in tasks if t['kind'] == 'registered_check']
    assert checks == ['python.compile']
    assert execute({'operation': checks[0], 'input_digest': frozen['digest']}, tmp_path)['exit_status'] == 0


def test_unregistered_recipe_check_refused():
    folder = {'checked_edits': {'checks': ['python.compile'], 'write_paths': ['app']}}
    with pytest.raises(ExecutionError, match='recipe_checks_unregistered'):
        registration(folder, {'manifest': [], 'digest': 'a'*64}, 'rev', 'builder', pin(['python.unittest']))


def test_recipe_pin_detects_content_change():
    value = pin(); value['definition']['checks'].append('python.unittest')
    with pytest.raises(ExecutionError, match='recipe_digest_conflict'):
        pinned_recipe(value)


def test_old_planned_context_keeps_all_registered_checks():
    folder = {'checked_edits': {'checks': ['python.compile', 'python.unittest'], 'write_paths': ['app']}}
    assert list(registration(folder, {'manifest': [], 'digest': 'a'*64}, 'rev')['checks']) == folder['checked_edits']['checks']


def test_fixer_recipe_must_include_final_unittest():
    folder = {'checked_edits': {'checks': ['python.compile', 'python.unittest'], 'write_paths': ['app']}}
    with pytest.raises(ExecutionError, match='fixer_unittest_required'):
        registration(folder, {'manifest': [], 'digest': 'a'*64}, 'rev', 'fixer', pin())


def test_direct_request_requires_recipe_pin_but_old_planned_request_does_not():
    request = {'contract_version': 2, 'team': 'configured', 'team_revision': 1, 'task': 'task',
               'request_key': 'start', 'budget': {'start_calls': 10, 'wall_mode': 'fixed', 'wall_seconds': 900},
               'worker_contract': 'checked-edits/1', 'acceptance': {'criteria': [
                   {'id': 'output', 'output_kind': 'proposal', 'checks': {'required_check_ids': ['python.compile']}}]}}
    assert start_request(request)['worker_contract'] == 'checked-edits/1'
    with pytest.raises(ExecutionError, match='recipe_pin_required'):
        start_request({**request, 'work_mode': 'builder'})
    assert start_request({**request, 'work_mode': 'builder', 'work_recipe': pin()})['work_recipe'] == pin()


def test_verbose_evidence_includes_errors_failures_and_subtests():
    out = ('test_bad (test_calc.Cases.test_bad) ... FAIL\n'
           'test_error (test_calc.Cases.test_error) ... ERROR\n'
           'test_good (test_calc.Cases.test_good) ... ok\n'
           'FAIL: test_sub (test_calc.Cases.test_sub) (n=1)\nRan 4 tests in 0.01s\n')
    result = unittest_evidence(out)
    assert result['tests_run'] == 4
    assert result['failing_test_ids'] == ['test_bad (test_calc.Cases.test_bad)',
                                         'test_error (test_calc.Cases.test_error)',
                                         'test_sub (test_calc.Cases.test_sub)']
    assert result['passed_test_ids'] == ['test_good (test_calc.Cases.test_good)']


def test_verbose_evidence_supports_docstrings_and_windows_newlines():
    result = unittest_evidence('test_good (test_calc.Cases.test_good)\r\nA documented case. ... ok\r\nRan 1 test in 0.01s\r\n')
    assert result['passed_test_ids'] == ['test_good (test_calc.Cases.test_good)']


@pytest.mark.parametrize('count,passed,exit_status,reason', [
    (2, ['broken'], 0, None),
    (1, ['broken'], 0, 'baseline_tests_missing'),
    (2, ['replacement'], 0, 'baseline_regression_not_repaired'),
    (2, ['broken'], 1, 'baseline_regression_not_repaired'),
])
def test_baseline_comparison(count, passed, exit_status, reason):
    baseline = {'tests_run': 2, 'passed': False, 'failing_test_ids': ['broken']}
    final = {'tests_run': count, 'passed_test_ids': passed, 'exit_status': exit_status}
    assert compare_baseline(baseline, final) == reason


def test_real_failing_baseline_and_successful_repair(tmp_path, capfd):
    app = tmp_path / 'app'; app.mkdir()
    source = app / 'calc.py'; source.write_text('def add(a,b): return a-b\n')
    (app / 'test_calc.py').write_text('import unittest\nfrom calc import add\nclass Cases(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n')
    baseline = execute({'operation': 'python.baseline', 'input_digest': snapshot(tmp_path)['digest']}, tmp_path)
    assert baseline['failing_test_ids'] == ['test_add (test_calc.Cases.test_add)']
    assert baseline['baseline_already_passing'] is False
    source.write_text('def add(a,b): return a+b\n')
    final = execute({'operation': 'python.unittest', 'test_evidence': True,
                     'input_digest': snapshot(tmp_path)['digest']}, tmp_path)
    assert compare_baseline(baseline, final) is None


def test_new_file_hiding_an_existing_failure_is_refused(tmp_path, capfd):
    app = tmp_path / 'app'; app.mkdir()
    (app / 'test_existing.py').write_text('import unittest\nclass Cases(unittest.TestCase):\n def test_bad(self): self.fail("broken")\n def test_good(self): pass\n')
    baseline = execute({'operation': 'python.baseline', 'input_digest': snapshot(tmp_path)['digest']}, tmp_path)
    # New tests execute in-process and can tamper with imported existing suites.
    (app / 'test_aaa.py').write_text('import unittest, test_existing\ntest_existing.Cases.test_bad = None\n')
    final = execute({'operation': 'python.unittest', 'test_evidence': True,
                     'input_digest': snapshot(tmp_path)['digest']}, tmp_path)
    assert final['exit_status'] == 0 and final['tests_run'] < baseline['tests_run']
    assert compare_baseline(baseline, final) == 'baseline_tests_missing'


def test_passing_baseline_is_marked_and_allowed(tmp_path, capfd):
    app = tmp_path / 'app'; app.mkdir()
    (app / 'test_ok.py').write_text('import unittest\nclass Cases(unittest.TestCase):\n def test_good(self): pass\n')
    baseline = execute({'operation': 'python.baseline', 'input_digest': snapshot(tmp_path)['digest']}, tmp_path)
    assert baseline['baseline_already_passing'] is True
    assert compare_baseline(baseline, {**baseline, 'exit_status': 0}) is None


def test_evidence_is_bounded_and_missing_legacy_evidence_fails_closed():
    out = ''.join(f'test_{i} (tests.C.test_{i}) ... ok\n' for i in range(513)) + 'Ran 513 tests in 1s\n'
    with pytest.raises(ExecutionError, match='test_evidence_limit'):
        unittest_evidence(out)
    assert compare_baseline({'tests_run': 2, 'passed': False}, {'tests_run': 2}) == 'baseline_receipt_unavailable'


@pytest.mark.parametrize('operation,status', [('python.baseline', 1), ('python.unittest', 0)])
def test_worker_collects_ids_before_the_output_tail(monkeypatch, operation, status):
    import io
    from core.tool_jobs import checked_runner
    output = (b'test_early (test_cases.C.test_early) ... ok\n' + b'log line\n' * 8000
              + (b'FAIL: test_bad (test_cases.C.test_bad)\n' if status else b'test_bad (test_cases.C.test_bad) ... ok\n')
              + b'Ran 2 tests in 0.1s\n')
    monkeypatch.setattr(checked_runner, 'snapshot', lambda root: {'digest': 'source'})
    monkeypatch.setattr(checked_runner.subprocess, 'Popen', lambda *a, **k:
                        SimpleNamespace(stdout=io.BytesIO(output), wait=lambda: status))
    result = execute({'operation': operation, 'input_digest': 'source', 'test_evidence': True})
    assert result['tests_run'] == 2
    assert 'test_early (test_cases.C.test_early)' in result['passed_test_ids']
    if status:
        assert result['failing_test_ids'] == ['test_bad (test_cases.C.test_bad)']
        assert result['baseline_already_passing'] is False


def test_old_unittest_receipt_shape_is_preserved(monkeypatch):
    import io
    from core.tool_jobs import checked_runner
    monkeypatch.setattr(checked_runner, 'snapshot', lambda root: {'digest': 'source'})
    monkeypatch.setattr(checked_runner.subprocess, 'Popen', lambda *a, **k:
                        SimpleNamespace(stdout=io.BytesIO(b'Ran 1 test in 0.1s\nOK\n'), wait=lambda: 0))
    assert execute({'operation': 'python.unittest', 'input_digest': 'source'}) == {
        'assertions': ['python.unittest'], 'exit_status': 0}


def models():
    return {key: {'backend': 'configured', 'model': model, 'enabled': True, 'exact_calls': True}
            for key, model in [('one', 'first'), ('two', 'second')]}


def test_configured_default_chosen_on_multimodel_backend():
    lane = SimpleNamespace(default_backend='configured', backends={'configured': SimpleNamespace(model='second')})
    assert configured_binding(models(), lane) == 'two'
    assert configured_binding(models(), lane, model='first') == 'one'


def test_ambiguous_model_binding_lists_candidates():
    lane = SimpleNamespace(default_backend='configured', backends={})
    with pytest.raises(ExecutionError) as error:
        configured_binding(models(), lane)
    assert error.value.code == 'configured_model_binding_required'
    assert error.value.detail['candidate_binding_ids'] == ['one', 'two']


def test_unavailable_configured_model_has_no_fallback():
    lane = SimpleNamespace(default_backend='configured', backends={'configured': SimpleNamespace(model='absent')})
    with pytest.raises(ExecutionError, match='configured_model_binding_required'):
        configured_binding(models(), lane)


@pytest.mark.asyncio
@pytest.mark.parametrize('face,mode,allowed', [('builder','fixer',False), ('fixer','builder',False),
    ('builder','builder',True), ('fixer','fixer',True), ('orchestrator','builder',True),
    ('orchestrator','fixer',True), (None,'fixer',True)])
async def test_verified_face_hint_binds_prepare_mode(face, mode, allowed):
    work = Workbench(SimpleNamespace(store=None)); work.prepare = AsyncMock(return_value={'prepared': True})
    if allowed:
        assert await work.command(None, 'work.prepare', {'mode': mode}, face=face) == {'prepared': True}
    else:
        with pytest.raises(ExecutionError, match='face_work_mode_denied'):
            await work.command(None, 'work.prepare', {'mode': mode}, face=face)
        work.prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_server_conversation_hint_is_used_and_cannot_be_overridden():
    work = Workbench(SimpleNamespace(store=None)); work.prepare = AsyncMock(return_value={})
    await work.command(None, 'work.prepare', {'mode': 'builder'}, face='builder', conversation_id='verified')
    work.prepare.assert_awaited_once_with(None, {'mode': 'builder', 'conversation_id': 'verified'})
    with pytest.raises(ExecutionError, match='work_conversation_mismatch'):
        await work.command(None, 'work.prepare', {'mode': 'builder', 'conversation_id': 'other'},
                           face='builder', conversation_id='verified')
