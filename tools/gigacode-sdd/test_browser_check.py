import json
from pathlib import Path
import tempfile
import unittest
from browser_check import evidence, BROWSER_TOOLS


class BrowserCheckTests(unittest.TestCase):
    def test_wrapped_calls_require_successful_results_and_new_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = root/'stdout.jsonl'
            events.write_text('\n'.join(json.dumps({'message': {'content': [
                {'type': 'tool_use', 'name': 'tool_call', 'id': str(i),
                 'input': {'name': name, 'arguments': {'secret': 'not-exported'}}}]}})
                for i, name in enumerate(BROWSER_TOOLS)))
            result = {'ok': True, 'events_path': str(events),
                      'tool_calls': {str(i): 'tool_call' for i in range(4)},
                      'tool_results': [{'tool_use_id': str(i), 'is_error': False,
                                        'has_content': True} for i in range(4)]}
            png = root/'stand.png'
            self.assertEqual(evidence(result, png)['status'], 'needs-inspection')
            png.write_bytes(b'\x89PNG\r\n\x1a\n' + b'fixture')
            self.assertEqual(evidence(result, png)['status'], 'verified')
            result['tool_results'][0]['is_error'] = True
            self.assertEqual(evidence(result, png)['status'], 'needs-inspection')
            result['tool_results'][0]['is_error'] = False
            result['ok'] = False
            self.assertEqual(evidence(result, png)['status'], 'needs-inspection')
            self.assertNotIn('not-exported', json.dumps(evidence(result, png)))

    def test_direct_calls_supported_without_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = {'tool_calls': {'a': BROWSER_TOOLS[0]}, 'tool_results': [
                {'tool_use_id': 'a', 'is_error': False, 'has_content': True}]}
            observed = evidence(result, Path(tmp)/'absent.png')
            self.assertTrue(observed['successful_browser_calls']['navigate'])
            self.assertEqual(observed['status'], 'needs-inspection')
