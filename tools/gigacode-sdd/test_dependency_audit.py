import json
from pathlib import Path
import tempfile
import unittest
from dependency_audit import assess


class OperationAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_unbound_configuration_is_evidence_not_a_download(self):
        self.write('backend/evidence/arbitrary-test/project/.npmrc', 'registry=http://127.0.0.1:43121')
        report = assess(self.root, ['nexus.example'], contours=['backend'])
        self.assertEqual(report['blocking'], [])
        self.assertEqual(len(report['observations']), 1)
        self.assertFalse(report['network_isolation_proven'])
        # The same file becomes a build input when a command actually uses it.
        report = assess(self.root, ['nexus.example'], operations=[{
            'cwd': 'backend/evidence/arbitrary-test/project', 'argv': ['npm', 'install']}])
        self.assertEqual(len(report['blocking']), 1)

    def test_fixture_name_cannot_exempt_active_build(self):
        self.write('evidence/package.json', json.dumps({'workspaces': ['cases/*']}))
        self.write('evidence/cases/negative/.npmrc', 'registry=https://public.example')
        report = assess(self.root, ['nexus.example'], contours=['evidence'])
        self.assertEqual(len(report['blocking']), 1)

    def test_maven_modules_remain_checked(self):
        self.write('backend/pom.xml', '<project><modules><module>odd-name</module></modules></project>')
        self.write('backend/odd-name/pom.xml', '<project><repositories><repository><url>https://public.example</url></repository></repositories></project>')
        report = assess(self.root, ['nexus.example'], contours=['backend'])
        self.assertEqual(report['blocking'][0]['path'], 'backend/odd-name/pom.xml')

    def test_explicit_custom_config_cannot_hide_in_fixture_directory(self):
        path = self.write('data/anything.cfg', 'registry=http://127.0.0.1:43121')
        report = assess(self.root, ['nexus.example'], operations=[{
            'argv': ['npm', '--userconfig=' + str(path), 'install']}])
        self.assertEqual(len(report['blocking']), 1)
        self.assertEqual(report['blocking'][0]['reasons'], ['explicit-operation-config'])

    def test_external_settings_checked_without_secret_values(self):
        with tempfile.TemporaryDirectory() as other:
            path = Path(other) / 'custom.cfg'
            path.write_text('<settings><servers><server><password>SECRET</password></server></servers><mirrors><mirror><url>https://nexus.example.evil</url></mirror></mirrors></settings>')
            report = assess(self.root, ['nexus.example'], operations=[{'argv': ['mvn', '-s', str(path), 'package']}])
            self.assertEqual(len(report['blocking']), 1)
            self.assertNotIn('SECRET', json.dumps(report))

    def test_selected_missing_config_fails_before_command(self):
        report = assess(self.root, ['nexus.example'], operations=[{'argv': ['mvn', '-s', 'missing', 'test']}])
        self.assertEqual(report['blocking'][0]['code'], 'invalid-dependency-config')

    def test_local_package_reference_selects_config(self):
        self.write('package.json', json.dumps({'dependencies': {'a': 'file:data/mock'}}))
        self.write('data/mock/.npmrc', 'registry=http://127.0.0.1:12345')
        self.assertEqual(len(assess(self.root, ['nexus.example'])['blocking']), 1)

    def test_broken_unbound_example_does_not_stop_build(self):
        self.write('examples/broken/package.json', '{')
        report = assess(self.root, ['nexus.example'])
        self.assertEqual(report['blocking'], [])
        self.assertEqual(report['observations'][0]['code'], 'invalid-dependency-config')

class StaticRecoveryTests(unittest.TestCase):
    def test_old_static_gate_archived_without_accepting_candidate(self):
        from types import SimpleNamespace
        from controller import save, read
        from dependency_recovery import recover_static_gate
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            work = root / 'workers/a'; work.mkdir(parents=True)
            (work / 'data').mkdir(); (work / 'data/.npmrc').write_text('registry=http://127.0.0.1:1234')
            folder = root / 'execution-cycles/scope'; folder.mkdir(parents=True)
            save(folder / 'fatal.json', {'scope': 'scope', 'type': 'Blocked',
                'error': 'Dependency sources are not Nexus-only: old finding'})
            candidate = {'scope': 'scope', 'workspace': str(work), 'stage': 'execute', 'status': 'running'}
            save(folder / 'model/candidate.json', candidate)
            runner = SimpleNamespace(root=root, audit=lambda w, **kw: assess(w, ['nexus.example']))
            recover_static_gate(runner, folder, 'scope')
            self.assertFalse((folder / 'fatal.json').exists())
            self.assertEqual(len(list(folder.glob('fatal-static-audit-*.json'))), 1)
            self.assertEqual(read(folder / 'model/candidate.json'), candidate)
            self.assertEqual(len(read(folder / 'dependency-reassessment.json')['candidates']), 1)

    def test_other_safety_failure_is_never_migrated(self):
        from types import SimpleNamespace
        from controller import save
        from dependency_recovery import recover_static_gate
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            save(folder / 'fatal.json', {'scope': 'scope', 'type': 'Blocked', 'error': 'Permission violation'})
            recover_static_gate(SimpleNamespace(), folder, 'scope')
            self.assertTrue((folder / 'fatal.json').exists())

    def test_competing_requests_go_back_to_model(self):
        from operator_questions import execution_control
        kind, request = execution_control(json.dumps({'scope_request': {'paths': ['a']}, 'service_request': {'id': 'b'}}))
        self.assertEqual(kind, 'request_repair')
        self.assertIn('one valid', request['instruction'])

class ControllerAuditTests(unittest.TestCase):
    def setUp(self):
        import test_execution_cycle
        self.case = test_execution_cycle.ExecutionPortfolioTests('test_verified_winner_cancels_only_peer_after_comparison_window')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_bad_candidate_does_not_cancel_verified_peer(self):
        from unittest.mock import patch
        from dependency_audit import DependencyConfigurationError
        from execution_cycle import MODELS
        case = self.case
        bad_roots = set()
        def session(root, *args, model=None, **kwargs):
            result = case.session(root, *args, model=model, **kwargs)
            if model == MODELS[0]:
                bad_roots.add(str(root))
            return result
        def audit(root, *args, **kwargs):
            if str(root) in bad_roots and not kwargs.get('inventory_only'):
                raise DependencyConfigurationError({'blocking': [{'path': '.npmrc', 'code': 'non-nexus-download'}]}, 'audit.json')
            return {'blocking': [], 'observations': []}
        with patch.object(case.runner, 'audit', side_effect=audit):
            result = case.run_task(session=session)
        self.assertEqual(result['model'], MODELS[1])
        self.assertFalse(case.runner.stop.is_set())
        self.assertFalse(list((case.runner.root/'execution-cycles').glob('*/fatal.json')))

    def test_static_fixture_does_not_hide_service_request(self):
        from unittest.mock import patch
        from operator_questions import WaitingForOperator
        from controller import Runner
        case = self.case
        case.primary_only = True
        case.task['writes'].append('backend/data')
        def session(root, *args, **kwargs):
            result = case.session(root, *args, **kwargs)
            path = Path(root) / 'backend/data/negative/.npmrc'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('registry=http://127.0.0.1:43211')
            result['text'] = json.dumps({'service_request': {'id': 'stand'}})
            return result
        with patch.object(case.runner, 'audit', side_effect=lambda root, **kw: Runner.audit(case.runner, root, **kw)), \
             patch.object(case.runner.services, 'prepare', return_value={'identity': 'service-id', 'question_id': 'q'}) as prepare:
            with self.assertRaises(WaitingForOperator):
                case.run_task(session=session)
        prepare.assert_called_once()
        record = next(v for v in case.records() if v.get('pending_control'))
        self.assertEqual(record['pending_control'][0], 'service_request')
        self.assertTrue(record['preserved_test_inputs'])
        self.assertTrue(Path(record['preserved_test_inputs'][0]['preserved']).is_file())

class CommandSemanticsTests(unittest.TestCase):
    def test_python_s_is_not_maven_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = assess(tmp, ['nexus.example'], operations=[{'argv': ['python3', '-s', 'check.py']}])
            self.assertEqual(report['blocking'], [])
            self.assertNotIn('check.py', report['active_configs'])

    def test_pip_constraint_is_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'constraints.in').write_text('a @ https://public.example/a.whl')
            report = assess(root, ['nexus.example'], operations=[{
                'argv': ['python3', '-m', 'pip', 'install', '-c', 'constraints.in', 'a']}])
            self.assertEqual(report['blocking'][0]['path'], 'constraints.in')

class PrivateInputTests(unittest.TestCase):
    def test_only_untracked_unbound_private_configs_are_preserved(self):
        import subprocess
        from types import SimpleNamespace
        from dependency_audit import preserve_generated_private_configs
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); work = root / 'work'; work.mkdir()
            subprocess.run(['git', 'init', '-q', str(work)], check=True)
            for name in ('.npmrc', 'data/generated/.npmrc', 'data/tracked/.npmrc'):
                path = work / name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('registry=https://nexus.example/npm\n//private/:_auth=SECRET')
            subprocess.run(['git', '-C', str(work), 'add', 'data/tracked/.npmrc'], check=True)
            report = assess(work, ['nexus.example'])
            runner = SimpleNamespace(logdir=lambda name: root / name)
            records = preserve_generated_private_configs(runner, work, report)
            self.assertEqual(len(records), 1)
            self.assertTrue((work / '.npmrc').exists())
            self.assertTrue((work / 'data/tracked/.npmrc').exists())
            self.assertFalse((work / 'data/generated/.npmrc').exists())
            preserved = Path(records[0]['preserved'])
            self.assertIn('SECRET', preserved.read_text())
            self.assertNotIn('SECRET', (root / 'private-test-inputs/receipt.json').read_text())
            if __import__('os').name != 'nt':
                self.assertEqual(preserved.stat().st_mode & 0o777, 0o600)

class OverrideTests(unittest.TestCase):
    def test_cli_override_cannot_replace_approved_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.npmrc').write_text('registry=https://nexus.example/npm')
            report = assess(tmp, ['nexus.example'], operations=[{
                'argv': ['npm', 'install', '--registry', 'https://public.example/npm']}])
            self.assertEqual(report['blocking'][0]['reasons'], ['operation-override'])
            self.assertEqual(report['blocking'][0]['host'], 'public.example')

    def test_invalid_json_shape_remains_a_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fixtures/package.json'; path.parent.mkdir()
            path.write_text('[]')
            report = assess(tmp, ['nexus.example'])
            self.assertEqual(report['observations'][0]['code'], 'invalid-dependency-config')


if __name__ == '__main__':
    unittest.main()
