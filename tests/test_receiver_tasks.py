import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from receiver_tasks import validate_request, request_token

def request():
 return dict(schema_version=1,request_id=str(uuid.uuid4()),provider='sbertrek',role='AN',target='receiver',delivery='demo',summary='AN: PoC',confirmation='Создать задачу на принимающей стороне')

class RequestTests(unittest.TestCase):
 def test_validation(self):
  r=request();self.assertEqual(validate_request(r,'demo'),r)
  for patch in [dict(delivery='other'),dict(key='RSCON-1'),dict(request_id='../../bad'),dict(role='BE'),dict(confirmation='')]:
   with self.subTest(patch=patch),self.assertRaises(ValueError):validate_request({**r,**patch},'demo')
 def test_sender_transport_is_not_publication(self):
  import commit_message_policy as p
  if not hasattr(p,'keys_for_paths'):self.skipTest('sender-only guard')
  r=request();base='quarters/2026-Q4/features/demo/deliveries/demo'
  files={'delivery-index.json':json.dumps({'deliveries':{'demo':{'path':base}}}),base+'/analysis-task-request.json':json.dumps(r)}
  paths=[base+'/requirements.md',base+'/analysis-task-request.json']
  with self.assertRaises(ValueError):p.keys_for_paths(paths,files.get)
  keys=p.keys_for_paths(paths,files.get,allow_receiver_requests=True)
  self.assertEqual(keys,{request_token(r)})
  p.require_valid_commit_message(request_token(r)+': Передать требования',keys)
  with self.assertRaises(ValueError):p.require_valid_commit_message('Передать требования',keys)
  with self.assertRaises(ValueError):p.keys_for_paths(paths+['requirements-exchange/demo/manifest.json'],files.get,allow_receiver_requests=True)

if __name__=='__main__':unittest.main()
