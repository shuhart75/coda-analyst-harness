import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('ontologyctl',ROOT/'scripts/ontologyctl.py')
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class OntologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.put('sources/base.md','Verified baseline')
        self.data={'schema_version':1,'coverage':{'scope':'Sample','exclusions':['Unknown roles']},
            'sources':[{'id':'base','path':'sources/base.md','kind':'baseline','locator':'v1','sha256':self.sha('sources/base.md')}], 'cards':[]}
        for cid,required in [('overview',True),('constraints',True),('approval',False)]:
            self.put(f'ontology/{cid}.md',f'# {cid}\nCurrent fact\n')
            self.data['cards'].append({'id':cid,'title':cid,'path':f'ontology/{cid}.md','phase':'current','topics':['approvals'] if not required else ['system'],
                'views':['executive','entities','scenarios','integrations','roadmap'],'source_ids':['base'],'required':required,
                'review':{'date':'2026-11-02','basis':'Compared evidence'},'content_sha256':self.sha(f'ontology/{cid}.md')})
        self.roadmap={'schema_version':1,'directions':[]}
        self.save()

    def put(self,path,text):
        p=self.root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)

    def sha(self,path):
        return hashlib.sha256((self.root/path).read_bytes()).hexdigest()

    def save(self):
        self.put('planning/roadmap/index.json',json.dumps(self.roadmap))
        for s in self.data['sources']:
            if s['id']=='plans':s['sha256']=self.sha(s['path'])
        self.put('ontology/catalog.json',json.dumps(self.data))

    def future(self,state='under-consideration',cid='future'):
        if not any(s['id']=='plans' for s in self.data['sources']):
            self.data['sources'].append({'id':'plans','path':'planning/roadmap/index.json','kind':'plan','locator':'Decision history','sha256':'0'*64})
        card=copy.deepcopy(self.data['cards'][-1]);card.update(id=cid,title=cid,path=f'planning/roadmap/{cid}.md',phase='future',source_ids=['base','plans'],topics=['roadmap'],required=False)
        self.put(card['path'],'# Proposed only\nFUTURE_MARKER\n');card['content_sha256']=self.sha(card['path'])
        self.data['cards'].append(card)
        self.roadmap['directions'].append({'id':cid,'title':cid,'card_id':cid,'path':card['path'],'decision_state':state,
            'maturity':'prototype','horizon':'2027','committed_date':None,'realization':'not-confirmed','delivery_paths':[],
            'superseded_by':None,'history':[{'date':'2026-11-02','state':state,'actor':'Analyst','scope':'Concept only','source_ids':['base']}]})
        self.save()

    def test_ontology_is_project_content_not_harness_state(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        try:
            from workspace import misplaced_project_paths
            self.assertIn('ontology', misplaced_project_paths(self.root))
        finally:
            sys.path.pop(0)

    def test_current_bundle_has_evidence_and_limits(self):
        text=m.bundle(self.root,topics=['approvals'])
        self.assertIn('Unknown roles',text);self.assertIn(self.sha('sources/base.md'),text)
        self.assertIn('# constraints',text);self.assertIn('# overview',text)
        self.assertEqual(text,m.bundle(self.root,topics=['approvals']))

    def test_future_is_opt_in_even_in_roadmap_view(self):
        self.future()
        self.assertNotIn('FUTURE_MARKER',m.bundle(self.root))
        with self.assertRaisesRegex(ValueError,'include-future'):m.bundle(self.root,view='roadmap')
        text=m.bundle(self.root,view='roadmap',include_future=True)
        self.assertIn('FUTURE_MARKER',text);self.assertIn('under-consideration',text)

    def test_deferred_cancelled_and_superseded_are_explicit_history(self):
        for state in ['deferred','cancelled']:
            with self.subTest(state=state):
                self.setUp();self.future(state)
                self.assertNotIn('FUTURE_MARKER',m.bundle(self.root,include_future=True))
                self.assertIn('FUTURE_MARKER',m.bundle(self.root,include_future=True,include_history=True))
        self.setUp();self.future(cid='old');self.future(cid='new')
        d=self.roadmap['directions'][0];d['decision_state']='superseded';d['history'][-1]['state']='superseded';d['superseded_by']='new';self.save()
        text=m.bundle(self.root,include_future=True)
        self.assertNotIn('Карточка: old;',text);self.assertIn('Карточка: new;',text)

    def test_stale_source_blocks_and_lists_dependents(self):
        self.put('sources/base.md','Changed source')
        status=m.read_model(self.root)[-1]
        self.assertEqual(status['stale_sources'],['base']);self.assertIn('approval',status['review_required'])
        with self.assertRaisesRegex(ValueError,'Review required'):m.bundle(self.root)

    def test_summary_change_requires_review(self):
        self.put('ontology/approval.md','Unreviewed summary')
        self.assertEqual(m.read_model(self.root)[-1]['changed_cards'],['approval'])
        with self.assertRaises(ValueError):m.bundle(self.root)

    def test_plan_change_requires_summary_review(self):
        self.future();p=self.root/'planning/roadmap/index.json';d=json.loads(p.read_text())
        d['directions'][0]['horizon']='2028';p.write_text(json.dumps(d))
        self.assertIn('future',m.read_model(self.root)[-1]['review_required'])

    def test_budget_fails_without_partial_stdout(self):
        result=subprocess.run([sys.executable,str(ROOT/'scripts/ontologyctl.py'),str(self.root),'bundle','--max-bytes','10'],capture_output=True,text=True)
        self.assertEqual(result.returncode,1);self.assertEqual(result.stdout,'');self.assertIn('No output produced',result.stderr)

    def test_missing_source(self):
        (self.root/'sources/base.md').unlink()
        with self.assertRaisesRegex(ValueError,'Missing file'):m.read_model(self.root)

    def test_path_escape_and_symlink(self):
        with self.assertRaisesRegex(ValueError,'Unsafe path'):m.path_in(self.root,'../secret')
        with tempfile.TemporaryDirectory() as other:
            target=Path(other)/'outside';target.write_text('Outside')
            (self.root/'link').symlink_to(target)
            with self.assertRaisesRegex(ValueError,'escapes'):m.path_in(self.root,'link')

    def test_current_cannot_be_proven_by_test_spec_alone(self):
        self.data['sources'][0]['kind']='test-spec';self.save()
        with self.assertRaisesRegex(ValueError,'do not prove'):m.read_model(self.root)

    def test_reference_card_is_labelled(self):
        self.data['cards'][-1]['phase']='reference';self.save()
        self.assertIn('категория: reference',m.bundle(self.root))

    def test_duplicate_id_and_unknown_reference(self):
        self.data['cards'].append(copy.deepcopy(self.data['cards'][0]));self.save()
        with self.assertRaisesRegex(ValueError,'duplicate ID'):m.read_model(self.root)
        self.data['cards'].pop();self.data['cards'][0]['source_ids']=['missing'];self.save()
        with self.assertRaisesRegex(ValueError,'missing source ID'):m.read_model(self.root)

    def test_missing_required_limitations(self):
        self.data['cards'][1]['required']=False;self.save()
        with self.assertRaisesRegex(ValueError,'mandatory'):m.read_model(self.root)

    def test_unknown_topic_or_no_eligible_match(self):
        with self.assertRaisesRegex(ValueError,'Unknown topic'):m.bundle(self.root,topics=['typo'])
        self.future('cancelled')
        with self.assertRaisesRegex(ValueError,'No eligible card'):m.bundle(self.root,topics=['roadmap'],include_future=True)

    def test_future_requires_registered_roadmap(self):
        self.future();self.roadmap['directions']=[];self.save()
        with self.assertRaisesRegex(ValueError,'Every future'):m.read_model(self.root)

    def test_history_and_delivery_evidence(self):
        self.future();d=self.roadmap['directions'][0];d['decision_state']='cancelled';self.save()
        with self.assertRaisesRegex(ValueError,'last decision'):m.read_model(self.root)
        d['decision_state']='under-consideration';d['realization']='realized';self.save()
        with self.assertRaisesRegex(ValueError,'needs deliveries'):m.read_model(self.root)

    def test_commitment_requires_own_basis(self):
        self.future();d=self.roadmap['directions'][0];d['committed_date']='2027-03-01';self.save()
        with self.assertRaisesRegex(ValueError,'commitment_basis'):m.read_model(self.root)
        d['commitment_basis']='Date explicitly accepted';d['commitment_source_ids']=['base'];self.save()
        m.read_model(self.root)

    def test_replacement_cycles(self):
        self.future(cid='a');self.future(cid='b')
        for d,target in zip(self.roadmap['directions'],['b','a']):
            d['decision_state']='superseded';d['history'][-1]['state']='superseded';d['superseded_by']=target
        self.save()
        with self.assertRaisesRegex(ValueError,'cycle'):m.read_model(self.root)

    def test_invalid_phase_and_date(self):
        self.data['cards'][0]['phase']='deployed';self.save()
        with self.assertRaisesRegex(ValueError,'invalid phase'):m.read_model(self.root)
        self.data['cards'][0]['phase']='current';self.data['cards'][0]['review']['date']='2026-02-31';self.save()
        with self.assertRaises(ValueError):m.read_model(self.root)

    def test_scaffold_is_empty_honest_and_merge_preserves_content(self):
        target=self.root/'scaffold'
        subprocess.run(['bash',str(ROOT/'scripts/scaffold-project.sh'),str(target)],check=True,capture_output=True)
        self.assertFalse(m.read_model(target)[-1]['initialized'])
        with self.assertRaisesRegex(ValueError,'no reviewed cards'):m.bundle(target)
        original=(target/'ontology/catalog.json').read_bytes()
        (target/'ontology/README.md').write_text('Keep my notes')
        subprocess.run(['bash',str(ROOT/'scripts/scaffold-project.sh'),str(target),'--merge'],check=True,capture_output=True)
        self.assertEqual(original,(target/'ontology/catalog.json').read_bytes())
        self.assertEqual('Keep my notes',(target/'ontology/README.md').read_text())


if __name__=='__main__':unittest.main()
