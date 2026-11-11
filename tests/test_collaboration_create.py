from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / 'scripts/workspace.py'
COLLABORATION = ROOT / 'scripts/collaboration.py'

def run(*args, env=None):
    return subprocess.run(args, text=True, capture_output=True, check=False, env=env)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import collaboration as api
from collaboration_create import create


class CreationTests(unittest.TestCase):
    def git(self, repository: Path, *args: str) -> str:
        result = run("git", "-C", str(repository), *args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def prepare_workspace(self, root: Path) -> tuple[Path, Path, Path, dict[str, str]]:
        seed = root / "seed"
        remote = root / "analytics.git"
        workspace = root / "harness"
        seed.mkdir()
        workspace.mkdir()
        self.assertEqual(run("git", "init", "-b", "main", str(seed)).returncode, 0)
        self.git(seed, "config", "user.name", "Harness Test")
        self.git(seed, "config", "user.email", "harness@example.test")
        feature = seed / "features/registry"
        feature.mkdir(parents=True)
        (feature / "requirements.md").write_text("# Требования\n\nИсходная версия.\n", encoding="utf-8")
        self.git(seed, "add", "--", "features/registry/requirements.md")
        self.git(seed, "commit", "-m", "Добавить требования")
        self.assertEqual(run("git", "clone", "--bare", str(seed), str(remote)).returncode, 0)
        self.git(remote, "symbolic-ref", "HEAD", "refs/heads/main")

        environment = {**os.environ, 'ANALYST_HARNESS_STATE_ROOT': str(workspace / '.workspace-state')}
        analytics = workspace / 'analytical-project'
        result = run('git', 'clone', str(remote), str(analytics))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.git(analytics, 'config', 'user.name', 'Harness Test')
        self.git(analytics, 'config', 'user.email', 'harness@example.test')
        runtime = workspace / '.workspace-state'
        runtime.mkdir()
        (runtime / 'workspace.json').write_text(json.dumps({
            'roles': {'analytics': {'repository': 'analytical-project', 'path': str(analytics)}}
        }))
        api.write_state(workspace, api.new_state('ivan'))
        return workspace, analytics, remote, environment

    def collaboration(
        self,
        workspace: Path,
        environment: dict[str, str],
        command: str,
        *arguments: str,
        expected: int = 0,
    ) -> dict:
        result = run(
            sys.executable,
            str(COLLABORATION),
            "--root",
            str(workspace),
            command,
            *arguments,
            env=environment,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)


    def setup_case(self, root):
        workspace, repo, remote, env = self.prepare_workspace(root)
        (repo / 'delivery-index.json').write_text(json.dumps({'schema_version': 1, 'deliveries': {}})+'\n')
        self.git(repo, 'add', 'delivery-index.json')
        self.git(repo, 'commit', '-m', 'Добавить квартальный реестр')
        self.git(repo, 'push', 'origin', 'main')
        args = api.parser().parse_args(['--root', str(workspace), 'create',
            '--feature', 'new-capability', '--delivery', 'poc', '--key', 'new-capability-poc',
            '--quarter', '2026-Q4', '--title', 'Новая возможность', '--analyst-confirmed'])
        return workspace, repo, remote, env, args

    def test_create_pins_branch_preserves_main_and_supports_requirement_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            w,r,remote,env,args=self.setup_case(Path(tmp))
            old=self.git(r,'rev-parse','main')
            create(args,api)
            self.assertEqual(self.git(r,'rev-parse','main'),old)
            self.assertEqual(self.git(remote,'rev-parse','main'),old)
            state=api.load_state(w)
            self.assertEqual(state['active_work']['status'],'active')
            self.assertEqual(api.current_branch(r),state['active_work']['branch'])
            api.require_active_work(w,r,state)
            index=json.loads((r/'delivery-index.json').read_text())
            self.assertTrue((r/index['deliveries'][args.key]['path']/'README.md').is_file())
            with self.assertRaises(ValueError): create(args,api)

    def test_interrupt_after_writes_resumes_only_identical_untampered_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            w,r,_,env,args=self.setup_case(Path(tmp))
            original=api.write_state
            def fail_last(root,state):
                if state['active_work']['status']=='active': raise OSError('injected final state failure')
                original(root,state)
            with patch.object(api,'write_state',side_effect=fail_last):
                with self.assertRaises(OSError): create(args,api)
            state=api.load_state(w)
            self.assertEqual(state['active_work']['status'],'creation-pending')
            with self.assertRaises(ValueError): api.require_active_work(w,r,state)
            args.quarter='2027-Q1'
            with self.assertRaises(ValueError): create(args,api)
            args.quarter='2026-Q4'
            index=r/'delivery-index.json'; expected=index.read_text()
            index.write_text(expected+' ')
            with self.assertRaises(ValueError): create(args,api)
            self.assertEqual(index.read_text(),expected+' ')
            index.write_text(expected)
            create(args,api)
            self.assertEqual(api.load_state(w)['active_work']['status'],'active')

    def test_interrupt_before_switch_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            w,r,_,env,args=self.setup_case(Path(tmp))
            original=api.git
            def fail_switch(repo,*argv):
                if argv[0]=='switch': raise OSError('injected switch failure')
                return original(repo,*argv)
            with patch.object(api,'git',side_effect=fail_switch):
                with self.assertRaises(OSError): create(args,api)
            self.assertEqual(api.current_branch(r),'main')
            create(args,api)
            self.assertEqual(api.load_state(w)['active_work']['status'],'active')

    def test_partial_file_write_resumes_without_replacing_other_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            w,r,_,env,args=self.setup_case(Path(tmp))
            original=Path.replace
            def fail_readme(source,target):
                if str(target).endswith('/deliveries/poc/README.md'):
                    raise OSError('injected second file failure')
                return original(source,target)
            with patch.object(Path,'replace',fail_readme):
                with self.assertRaises(OSError): create(args,api)
            self.assertIn(args.key,json.loads((r/'delivery-index.json').read_text())['deliveries'])
            self.assertEqual(api.load_state(w)['active_work']['status'],'creation-pending')
            create(args,api)
            self.assertEqual(api.load_state(w)['active_work']['status'],'active')

    def test_dirty_invalid_duplicate_and_failed_fetch_do_not_create_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            w,r,_,env,args=self.setup_case(Path(tmp))
            args.quarter='bad'
            with self.assertRaises(ValueError): create(args,api)
            args.quarter='2026-Q4'
            (r/'user.txt').write_text('keep')
            with self.assertRaises(ValueError): create(args,api)
            self.assertEqual((r/'user.txt').read_text(),'keep')
            (r/'user.txt').unlink()
            with patch.object(api,'fetch_main',side_effect=ValueError('offline')):
                with self.assertRaises(ValueError): create(args,api)
            self.assertIsNone(api.load_state(w).get('active_work'))
            self.assertEqual(api.current_branch(r),'main')
            # Existing identity on accepted main is not silently reused.
            index={'schema_version':1,'deliveries':{'existing':{'feature_id':args.feature}}}
            (r/'delivery-index.json').write_text(json.dumps(index))
            self.git(r,'add','delivery-index.json');self.git(r,'commit','-m','Зарегистрировать идентичность')
            self.git(r,'push','origin','main')
            with self.assertRaises(ValueError): create(args,api)
            self.assertIsNone(api.load_state(w).get('active_work'))

if __name__=='__main__': unittest.main()
