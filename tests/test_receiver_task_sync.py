import json
import sys
from pathlib import Path
from tests.test_source_import import SourceImportTests
from tests.test_receiver_tasks import request
from tests import test_workspace as fixture
sys.path.insert(0,str(fixture.ROOT/'scripts'))
from receiver_tasks import operate

class ReceiverTaskSyncTests(SourceImportTests):
 def test_request_blocks_push_then_binding_resumes_same_import(self):
  r=request();base='quarters/2026-Q4/features/demo/deliveries/demo'
  folder=self.source/base;folder.mkdir(parents=True)
  (folder/'analysis-task-request.json').write_text(json.dumps(r)+'\n')
  (folder/'requirements.md').write_text('# Demo\n')
  index={'schema_version':1,'deliveries':{'demo':{'feature_id':'demo','delivery_id':'demo','quarter':'2026-Q4','path':base}}}
  (self.source/'delivery-index.json').write_text(json.dumps(index)+'\n')
  self.git(self.source,'add','--','delivery-index.json',base+'/analysis-task-request.json',base+'/requirements.md')
  self.git(self.source,'commit','-m','Request receiver task');self.git(self.source,'push','origin','main')
  before=self.git(self.documents,'rev-parse','HEAD')
  first=self.sync()['analytics_exchange']['source_import']
  self.assertEqual(first['status'],'receiver-tasks-required')
  self.assertNotIn(first['request_branch'],self.git(self.documents,'ls-remote','--heads','origin'))
  again=self.sync()['analytics_exchange']['source_import'];self.assertEqual(first['request_commit'],again['request_commit'])
  self.assertTrue(operate(self.workspace,'claim',r['request_id'])['create_allowed'])
  self.assertFalse(operate(self.workspace,'claim',r['request_id'])['create_allowed'])
  evidence=self.root/'evidence.json';evidence.write_text(json.dumps({'key':'RSCON-123','role':'AN'}))
  bound=operate(self.workspace,'bind',r['request_id'],'RSCON-123',evidence)
  self.assertEqual(bound['status'],'bound')
  third=self.sync()['analytics_exchange']['source_import']
  self.assertEqual(third['status'],'awaiting-merge');self.assertEqual(third['request_branch'],first['request_branch'])
  self.assertEqual(before,self.git(self.documents,'rev-parse','HEAD'))
  self.assertEqual(operate(self.workspace,'bind',r['request_id'],'RSCON-123',evidence)['status'],'already-bound-or-not-pending')
