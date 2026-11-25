"""Compatibility must not turn the candidate itself into trusted evidence."""
import copy
import importlib.util
import io
from contextlib import redirect_stderr
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import project_layout
import delivery_stages
import sdd_bundle
spec = importlib.util.spec_from_file_location('exchange_recovery', ROOT / 'scripts/requirements-exchange.py')
exchange = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exchange)


class DeliveryRecoveryTests(unittest.TestCase):
    def test_contract_compatibility_preserves_machine_rules(self):
        expected = exchange.requested_returns_contract(5)
        historical = copy.deepcopy(expected)
        historical['tasks']['jira_key'] = historical['tasks'].pop('sbertrek_key')
        historical['tasks']['meaning'] = 'Old description'
        historical['task_results']['frequency'] = 'Historical explanation of when to report'
        del historical['tasks']['contour_rule']
        self.assertTrue(exchange.contract_matches(historical, expected, legacy=True))
        self.assertFalse(exchange.contract_matches(historical, expected))
        for section, field, value in [('tasks', 'path', '../outside.md'),
                                      ('receipt', 'required_before_other_returns', False),
                                      ('tasks', 'local_sdd_reference', 'optional')]:
            broken = copy.deepcopy(historical); broken[section][field] = value
            self.assertFalse(exchange.contract_matches(broken, expected, legacy=True))
        broken = copy.deepcopy(historical); broken['receipt'] = {}
        self.assertFalse(exchange.contract_matches(broken, expected, legacy=True))
        self.assertFalse(exchange.contract_matches({'required': 0, 'results': 'analyst-owned-evidence-review-v1'}, exchange.requested_returns_contract(6)))

    def test_historical_binding_requires_registered_same_stream(self):
        current = dict(feature_id='new', delivery_id='demo', delivery_key='demo', quarter='2026-Q4')
        old = dict(current, feature_id='old')
        packet = dict(schema_version=6, delivery_binding=old, revisions=[{'delivery_binding': old}])
        with patch.object(project_layout, 'exchange_binding', return_value=current):
            with self.assertRaisesRegex(ValueError, 'different feature'):
                project_layout.require_manifest_binding(ROOT, 'demo', packet)
            project_layout.require_manifest_binding(ROOT, 'demo', packet, historical=[old])
            foreign = dict(old, quarter='2026-Q3')
            with self.assertRaisesRegex(ValueError, 'different feature'):
                project_layout.require_manifest_binding(ROOT, 'demo', dict(packet, delivery_binding=foreign), historical=[foreign])
        records = [{'entry': {'delivery_binding': old}, 'publication_confirmed': False}]
        self.assertEqual(exchange.registered_bindings(records), [])

    def test_manifest_cannot_invent_a_historical_owner_revision(self):
        current = dict(feature_id='new', delivery_id='demo', delivery_key='demo', quarter='2026-Q4')
        entry = dict(revision=1, delivery_binding=dict(current, feature_id='old'))
        packet = dict(delivery_binding=current, revisions=[entry])
        with patch.object(delivery_stages, 'registry', return_value={'revisions': [], 'stages': []}):
            with self.assertRaisesRegex(ValueError, 'confirmed identical revision'):
                delivery_stages.require_manifest_history({}, packet)

    def test_cleanup_error_is_reported_and_never_masks_body_failure(self):
        warnings = []
        with patch.object(tempfile.TemporaryDirectory, '_rmtree', side_effect=OSError('directory busy')), redirect_stderr(io.StringIO()) as err:
            with exchange.DisposablePublicationClone(cleanup_warnings=warnings) as root:
                self.addCleanup(__import__('shutil').rmtree, root)
                result = {'status': 'accepted', 'cleanup_warnings': warnings}
            self.assertEqual(result['status'], 'accepted')
            self.assertEqual(result['cleanup_warnings'][0]['path'], root)
            self.assertIn('cleanup failed', err.getvalue())
            with self.assertRaisesRegex(ValueError, 'push rejected'):
                with exchange.DisposablePublicationClone(cleanup_warnings=[]) as root:
                    self.addCleanup(__import__('shutil').rmtree, root)
                    raise ValueError('push rejected')

    def test_finder_metadata_is_ignored_but_links_are_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'spec.md').write_text('spec')
            before = sdd_bundle._files(root)
            (root/'.DS_Store').write_bytes(b'finder')
            self.assertEqual(sdd_bundle._files(root), before)
            (root/'.DS_Store').unlink()
            try:
                (root/'.DS_Store').symlink_to(root/'spec.md')
            except OSError:
                self.skipTest('Symlinks unavailable')
            with self.assertRaisesRegex(ValueError, 'non-regular'):
                sdd_bundle._files(root)
