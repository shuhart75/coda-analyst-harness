import unittest
from operator_display import render_question


class OperatorDisplayTests(unittest.TestCase):
    def question(self, **extra):
        return dict(id='question-id', task='backend-its', topic='business-choice',
                    question='Какое имя использовать?', requests=[], **extra)

    def test_scope_decision_is_readable_and_evidence_is_only_linked(self):
        q = self.question(action={'kind': 'scope-repair-v1', 'files': [
            {'path': 'backend/Test.java', 'owners': []}]})
        q['requests'] = [{'details': {'reason': 'Тесту не хватает новой зависимости.',
            'reported_context': {'raw': 'JSON_DIAGNOSTIC_DUPLICATE'*100}}}]
        text = render_question('/run', q)
        for phrase in ('Разрешить исправить', 'backend/Test.java', 'не закреплён ни за одной задачей',
                       'Тесту не хватает новой зависимости.', 'При согласии:', 'При отказе:', 'Подробности:'):
            self.assertIn(phrase, text)
        self.assertNotIn('JSON_DIAGNOSTIC', text)
        self.assertNotIn('{', text)
        self.assertLess(len(text.splitlines()), 12)

    def test_legacy_unsupported_question_explains_that_yes_does_not_grant_scope(self):
        q = self.question()
        q['topic'] = 'scope-unavailable-old'
        q['requests'] = [{'details': {'reason': 'No accepted predecessor owns the requested file: backend/Test.java',
            'model_request': {'request': 'Исправить один тест?', 'diagnosis': 'Тесту не хватает зависимости.',
                              'details': {'context': {'request': 'DUPLICATE'}}}}}]
        text = render_question('/run', q)
        self.assertIn('Модель просит: Исправить один тест?', text)
        self.assertIn('Ответ «да» не расширит область записи', text)
        self.assertNotIn('No accepted', text)
        self.assertNotIn('DUPLICATE', text)

    def test_service_approval_keeps_command_directory_ports_and_purpose(self):
        q = self.question()
        q['topic'] = 'service:backend'
        q['requests'] = [{'details': {'command': ['python3', 'serve.py', '--port', '8082'],
            'cwd': '/run/frozen', 'ports': [8082], 'purpose': 'Проверить API',
            'artifacts_sha256': {'serve.py': 'INTERNAL_HASH'}}}]
        text = render_question('/run', q)
        for phrase in ('python3 serve.py --port 8082', '/run/frozen', '8082', 'Проверить API'):
            self.assertIn(phrase, text)
        self.assertNotIn('INTERNAL_HASH', text)
