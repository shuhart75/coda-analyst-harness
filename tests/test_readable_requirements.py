"""New authoring profile through guards, grooming and standalone exchange."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import readable_requirements as readable
import grooming
import sdd_bundle

TEXT = (ROOT / 'tests/fixtures/readable-requirements.md').read_text(encoding='utf-8')


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ReadableRequirementsTests(unittest.TestCase):
    def test_table_heading_and_shared_acceptance(self):
        self.assertEqual(readable.validate(TEXT), [])
        self.assertEqual(readable.authored_ids(TEXT), ['REQ-DEMO-001', 'REQ-DEMO-002'])
        self.assertEqual(sdd_bundle.authored_requirement_ids(TEXT), readable.authored_ids(TEXT))
        self.assertEqual(load('requirements-exchange').validate_requirements_text(TEXT), [])

    def test_missing_unknown_duplicate_and_empty_acceptance_fail(self):
        cases = [
            (TEXT.replace('REQ-DEMO-001, REQ-DEMO-002', 'REQ-DEMO-001'), 'нет критерия'),
            (TEXT.replace('REQ-DEMO-001, REQ-DEMO-002', 'REQ-DEMO-001, REQ-UNKNOWN'), 'неизвестное'),
            (TEXT.replace('REQ-DEMO-002. Результаты', 'REQ-DEMO-001. Результаты'), 'уникальными'),
            (TEXT.replace('Видны две строки с номерами; выбран только второй документ', ''), 'критерий требует'),
            (TEXT.replace('REQ-DEMO-001, REQ-DEMO-002', 'REQ-DEMO-001–002'), 'без диапазонов'),
            (TEXT.replace('## Критерии приёмки', '## Справка'), 'нет критерия'),
            (TEXT.replace('| AC-001', '| AC-001', 1) + '\n## Критерии приёмки\n', 'один раздел'),
        ]
        for text, error in cases:
            with self.subTest(error=error):
                self.assertTrue(any(error in e for e in readable.validate(text)), readable.validate(text))
                with self.assertRaises(ValueError):
                    sdd_bundle.authored_requirement_ids(text)

    def test_examples_and_comments_cannot_supply_missing_coverage(self):
        row = next(line for line in TEXT.splitlines() if line.startswith('| AC-001'))
        for wrapper in ('```markdown\n%s\n```', '~~~markdown\n%s\n~~~', '<!--\n%s\n-->'):
            text = TEXT.replace(row, wrapper % row)
            self.assertIn('REQ-DEMO-001: нет критерия приёмки', readable.validate(text))
        fake = '\n```markdown\n### REQ-FAKE. Пример\nТекст\n```\n'
        self.assertEqual(readable.validate(TEXT + fake), [])

    def test_duplicate_acceptance_is_rejected(self):
        row = next(line for line in TEXT.splitlines() if line.startswith('| AC-001'))
        self.assertIn('критерии AC-* должны быть уникальными', readable.validate(TEXT.replace(row, row+'\n'+row)))

    def test_table_requires_title_and_rule_and_preserves_escaped_pipe(self):
        self.assertTrue(readable.validate(TEXT.replace('Выбор документа | ', '')))
        self.assertEqual(readable.validate(TEXT.replace('радиокнопкой.', r'радиокнопкой с меткой A\|B.')), [])

    def test_both_cli_guards_check_new_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            path = project/'features/demo/requirements.md'
            path.parent.mkdir(parents=True)
            for script in ('validate-requirements-profile', 'validate-requirements-wording'):
                path.write_text(TEXT, encoding='utf-8')
                command = [sys.executable, str(ROOT/'scripts'/f'{script}.py'), str(project), '--feature', 'demo']
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                bad = TEXT.replace('REQ-DEMO-001, REQ-DEMO-002', 'REQ-DEMO-001') if script.endswith('profile') else TEXT.replace('Пользователь выбирает', 'Любая роль выбирает')
                path.write_text(bad, encoding='utf-8')
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_grooming_retains_shared_criterion_and_binds_changes(self):
        blocks = grooming.requirement_blocks(TEXT)
        self.assertEqual(blocks['REQ-DEMO-001']['title'], 'Выбор документа')
        self.assertEqual(set(blocks['REQ-DEMO-001']['scenarios']), {'AC-001'})
        self.assertEqual(blocks['REQ-DEMO-001']['scenarios'], blocks['REQ-DEMO-002']['scenarios'])
        task = {'requirements': [{'req_id': req, 'scenarios': ['AC-001']} for req in blocks]}
        original = grooming.task_source_hash(task, blocks)
        changed = grooming.requirement_blocks(TEXT.replace('выбран только второй документ', 'выбран первый документ'))
        self.assertNotEqual(original, grooming.task_source_hash(task, changed))

    def test_table_acceptance_uses_wording_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'requirements.md'
            path.write_text(TEXT.replace('Видны две строки с номерами; выбран только второй документ',
                                         'Система должна показать результат'), encoding='utf-8')
            findings = load('validate-requirements-wording').validate(path)
            self.assertTrue(any(code == 'нормативный-результат' and owner == 'AC-001'
                                for _, code, _, owner in findings), findings)

    def test_grooming_renders_rules_and_shared_acceptance(self):
        from test_grooming import make_plan
        package = {'changes': [{'contour': 'backend', 'change_id': 'show-result',
                               'coverage': [{'req_id': r} for r in readable.authored_ids(TEXT)]}]}
        plan = make_plan(TEXT, package)
        files = grooming.files_for(plan, TEXT, package)
        task = files['be-01/requirements.md']
        self.assertIn('| REQ-DEMO-001 | Выбор документа |', task)
        self.assertIn('### REQ-DEMO-002. Результаты поиска', task)
        self.assertIn('| AC-001 |', task)

    def test_exchange_installs_parser_for_isolated_receiver(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'exchange'
            root.mkdir()
            load('requirements-exchange').install_root_contract(root)
            (root/'input.md').write_text(TEXT, encoding='utf-8')
            command = 'from pathlib import Path; import sdd_bundle, grooming; t=Path("input.md").read_text(encoding="utf-8"); assert len(sdd_bundle.authored_requirement_ids(t))==2; assert len(grooming.requirement_blocks(t))==2'
            result = subprocess.run([sys.executable, '-c', command], cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main()
