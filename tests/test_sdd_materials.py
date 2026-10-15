"""Materials use the same audit -> publication -> receiver -> receipt boundary as SDD."""
import hashlib
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_sdd_exchange as exchange_tests
import test_sdd_receive as receive_tests
import sdd_bundle
import sdd_audit


def write_materials(root):
    root.mkdir()
    (root / 'config').mkdir()
    (root / 'config' / 'example.json').write_text('{"version":"v1"}\n')
    (root / 'START.md').write_text('Read the accepted requirements.\n')
    refresh(root)


def refresh(root):
    (root / 'SHA256SUMS').write_text(''.join(
        hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.relative_to(root).as_posix() + '\n'
        for p in sorted(root.rglob('*')) if p.is_file() and p.name != 'SHA256SUMS'))


class MaterialsExchangeTests(exchange_tests.SddExchangeTests):
    # Inherit the existing publication tests unchanged, with their legacy schema-1 inputs.
    def enable_materials(self):
        self.materials = self.feature / 'materials'
        write_materials(self.materials)
        package = json.loads((self.sdd / 'package.json').read_text())
        package['schema_version'] = 2
        (self.sdd / 'package.json').write_text(json.dumps(package))

    def test_materials_isolated_code_publication(self):
        self.enable_materials()
        self.enable_code_exchange()
        before = self.code_snapshot()
        self.authorize()
        prepared = self.prepare()
        self.assertEqual(prepared['status'], 'awaiting-merge')
        self.assertFalse(prepared['publication_confirmed'])
        for path in sorted(self.materials.rglob('*')):
            if path.is_file():
                remote_path = 'requirements-exchange/demo/revisions/001/materials/' + path.relative_to(self.materials).as_posix()
                self.assertEqual(self.git(self.remote, 'show', prepared['request_commit'] + ':' + remote_path), path.read_text().strip())
        self.assertEqual(self.code_snapshot(), before)
        self.accept(prepared)
        self.assertTrue(self.prepare()['publication_confirmed'])
        self.assertEqual(self.code_snapshot(), before)

    def test_full_publish_receive_and_receipt(self):
        self.enable_materials()
        self.authorize()
        prepared = self.prepare()
        packet = Path(prepared['manifest']).parent
        revision = Path(prepared['requirements']).parent
        expected = sdd_bundle.inspect(self.sdd, (self.feature / 'requirements.md').read_text())
        self.assertEqual(self.manifest(prepared)['revisions'][0]['sdd_input'], expected)
        self.assertEqual(sdd_audit.working_hash(self.feature), expected['sha256'])
        self.assertEqual(sdd_bundle.materials_descriptor(revision / 'materials'), expected['materials'])
        result = receive_tests.receiver.receive(packet, prepared['revision'], self.code, apply=True)
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(result['materials']['sha256'], expected['materials']['sha256'])
        receipt_path = self.write_receipt(prepared)
        receipt = json.loads(receipt_path.read_text())
        receipt.update(schema_version=2, sdd_sha256=expected['sha256'])
        receipt_path.write_text(json.dumps(receipt))
        self.validate()

    def test_material_change_invalidates_audit_and_working_hash(self):
        self.enable_materials()
        self.begin()
        self.audit()
        before = sdd_audit.working_hash(self.feature)
        (self.materials / 'START.md').write_text('Changed instructions.\n')
        self.assertNotEqual(before, sdd_audit.working_hash(self.feature))
        refresh(self.materials)
        self.state('confirm-audit', success=False)

    def test_material_change_requires_new_revision_and_new_receipt(self):
        self.enable_materials()
        self.authorize()
        first = self.prepare()
        first_root = Path(first['requirements']).parent
        first_bytes = {p.relative_to(first_root).as_posix(): p.read_bytes() for p in first_root.rglob('*') if p.is_file()}
        first_digest = self.manifest(first)['revisions'][0]['sdd_input']['sha256']
        (self.materials / 'START.md').write_text('New instructions.\n')
        refresh(self.materials)
        self.prepare(success=False)
        self.authorize()
        second = self.prepare()
        self.assertEqual(second['revision'], 2)
        second_digest = self.manifest(second)['revisions'][1]['sdd_input']['sha256']
        self.assertNotEqual(first_digest, second_digest)
        self.assertEqual(first_bytes, {p.relative_to(first_root).as_posix(): p.read_bytes() for p in first_root.rglob('*') if p.is_file()})
        receipt_path = self.write_receipt(second)
        receipt = json.loads(receipt_path.read_text())
        receipt.update(schema_version=2, sdd_sha256=first_digest)
        receipt_path.write_text(json.dumps(receipt))
        self.validate(success=False)
        receipt['sdd_sha256'] = second_digest
        receipt_path.write_text(json.dumps(receipt))
        self.validate()

    def test_no_silent_omission_in_schema_one(self):
        write_materials(self.feature / 'materials')
        with self.assertRaisesRegex(ValueError, 'schema_version 2'):
            sdd_bundle.inspect(self.sdd, (self.feature / 'requirements.md').read_text())

    def test_copy_failure_rolls_back_both_directories(self):
        self.enable_materials()
        text = (self.feature / 'requirements.md').read_text()
        descriptor = sdd_bundle.inspect(self.sdd, text)
        dst = self.parent / 'snapshot' / 'sdd'
        original_copy = shutil.copytree
        def broken(src, dest, *args, **kwargs):
            if Path(src) == self.materials:
                (Path(dest) / 'partial.txt').write_text('partial')
                raise OSError('injected failure')
            return original_copy(src, dest, *args, **kwargs)
        with patch.object(sdd_bundle.shutil, 'copytree', side_effect=broken):
            with self.assertRaisesRegex(OSError, 'injected'):
                sdd_bundle.copy_snapshot(self.sdd, dst, descriptor, text)
        self.assertFalse(dst.exists())
        self.assertFalse((dst.parent / 'materials').exists())


class MaterialsReceiverTests(receive_tests.SddReceiveTests):
    def setUp(self):
        super().setUp()
        self.materials = self.bundle.parent / 'materials'
        write_materials(self.materials)
        self.package['schema_version'] = 2
        self.save_packet()

    def test_missing_changed_extra_and_missing_checksum_rejected_before_write(self):
        for damage in ['directory', 'file', 'extra', 'bytes', 'checksum', 'symlink', 'unsafe', 'empty-directory']:
            with self.subTest(damage=damage):
                backup = self.root / 'backup'
                shutil.copytree(self.materials, backup)
                if damage == 'directory': shutil.rmtree(self.materials)
                elif damage == 'file': (self.materials / 'START.md').unlink()
                elif damage == 'extra': (self.materials / 'unexpected.txt').write_text('x')
                elif damage == 'bytes': (self.materials / 'START.md').write_text('changed')
                elif damage == 'checksum': (self.materials / 'SHA256SUMS').unlink()
                elif damage == 'symlink':
                    (self.materials / 'START.md').unlink()
                    (self.materials / 'START.md').symlink_to(backup / 'START.md')
                elif damage == 'unsafe':
                    with (self.materials / 'SHA256SUMS').open('a') as f: f.write('0'*64 + '  ../outside\n')
                elif damage == 'empty-directory': (self.materials / 'empty').mkdir()
                with self.assertRaises((ValueError, OSError)):
                    self.receive(apply=True)
                self.assertFalse(self.target().exists())
                if self.materials.exists(): shutil.rmtree(self.materials)
                backup.rename(self.materials)

    def test_recomputed_transport_checksum_does_not_bypass_revision_hash(self):
        (self.materials / 'START.md').write_text('altered and rehashed')
        refresh(self.materials)
        with self.assertRaisesRegex(ValueError, 'descriptor mismatch'):
            self.receive(apply=True)
        self.assertFalse(self.target().exists())

    def test_material_change_during_import_rolls_back_changes(self):
        original = receive_tests.receiver.ensure_parents
        def alter(*args):
            original(*args)
            (self.materials / 'START.md').write_text('changed during apply')
        with patch.object(receive_tests.receiver, 'ensure_parents', side_effect=alter):
            with self.assertRaises(ValueError): self.receive(apply=True)
        self.assertFalse(self.target().exists())

    def test_schema_one_descriptor_cannot_acknowledge_schema_two(self):
        self.manifest['revisions'][0]['sdd_input'].pop('materials')
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'descriptor mismatch'):
            self.receive(apply=True)


if __name__ == '__main__':
    unittest.main()
