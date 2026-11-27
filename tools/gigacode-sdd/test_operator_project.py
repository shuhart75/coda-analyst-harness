import threading
import time
import unittest
from unittest.mock import patch
from operator_project import ProjectLinks, project_urls, reachable


class ProjectLinkTests(unittest.TestCase):
    def records(self, url='http://127.0.0.1:3000/project'):
        return [{'manual_check': {'url': url}, 'created_at': 1}]

    def test_uses_explicit_ui_urls_and_newest_route_per_server(self):
        records = self.records()+[{'manual_check': {'url': 'http://127.0.0.1:3000/new'}, 'created_at': 2}]
        self.assertEqual(project_urls(records), ['http://127.0.0.1:3000/new'])
        for url in ['javascript:alert(1)', 'http://example.com/', 'http://user:secret@localhost/', 'http://localhost:bad/']:
            self.assertEqual(project_urls(self.records(url)), [])

    def test_slow_probe_does_not_block_status_or_duplicate_workers(self):
        started, release = threading.Event(), threading.Event()
        def probe(url):
            started.set()
            release.wait(2)
            return True
        links = ProjectLinks()
        with patch('operator_project.reachable', side_effect=probe) as call:
            begin=time.monotonic()
            self.assertFalse(links.view('run', self.records())[0]['available'])
            self.assertLess(time.monotonic()-begin, .2)
            self.assertTrue(started.wait(1))
            links.view('run', self.records())
            self.assertEqual(call.call_count, 1)
            release.set()
            for _ in range(100):
                if not links.pending:
                    break
                time.sleep(.01)
            self.assertTrue(links.view('run', self.records())[0]['available'])
        links.close()
        self.assertFalse(links.view('other-run', self.records())[0]['available'])

    def test_expired_success_is_not_live_and_unavailability_replaces_it(self):
        links=ProjectLinks()
        url=self.records()[0]['manual_check']['url']
        links.cache[('run',url)]={'available': True, 'checked_at': time.time()-31}
        links.close()
        self.assertFalse(links.view('run',self.records())[0]['available'])

    def test_browser_paths_are_encoded_and_invalid_http_is_not_live(self):
        from http.client import InvalidURL
        self.assertEqual(project_urls(self.records('http://localhost:3000/my page')), ['http://localhost:3000/my%20page'])
        with patch('urllib.request.OpenerDirector.open', side_effect=InvalidURL('bad response')):
            self.assertFalse(reachable('http://localhost:3000/'))

    def test_connection_failure_is_not_live(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=OSError('refused')):
            self.assertFalse(reachable('http://localhost:3000/'))
