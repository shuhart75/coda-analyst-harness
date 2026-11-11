import json
import sys
import unittest
import uuid
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from receiver_tasks import validate_request, requests_for_paths

def request():
    return dict(schema_version=1, request_id=str(uuid.uuid4()), provider='sbertrek',
                role='AN', target='receiver', delivery='demo', summary='PoC',
                confirmation='Создать задачу на принимающей стороне')

class RequestTests(unittest.TestCase):
    def test_validation(self):
        r = request()
        self.assertEqual(validate_request(r, 'demo'), r)
        for patch in [dict(delivery='other'), dict(key='RSCON-1'), dict(request_id='../../bad'),
                      dict(role='BE'), dict(confirmation='')]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_request({**r, **patch}, 'demo')

    def test_affected_scope_and_owner(self):
        base = 'quarters/2026-Q4/features/demo/deliveries/demo'
        r = request()
        files = {'delivery-index.json': json.dumps({'deliveries': {'demo': {'path': base}}}),
                 base + '/analysis-task-request.json': json.dumps(r)}
        self.assertEqual(requests_for_paths(['context/other.md'], files.get), [])
        got = requests_for_paths([base + '/requirements.md'], files.get)
        self.assertEqual(got[0]['request_id'], r['request_id'])
        self.assertEqual(got[0]['kind'], 'delivery')
        self.assertEqual(got[0]['owner'], {'type': 'delivery', 'id': 'demo'})
        # Request remains metadata, never a usable binding.
        from commit_tasks import validate_task
        with self.assertRaises(ValueError):
            validate_task(got[0], 'delivery')
