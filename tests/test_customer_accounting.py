from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from customer_accounting import validate_registry, build_report, check_registry_scope, read_json
from execution_collaboration import path_kind


class CustomerAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'features/a').mkdir(parents=True)
        (self.root / 'features/b').mkdir()
        (self.root / 'evidence.md').write_text('Analyst-approved source')
        self.registry = {'schema_version':1, 'customers':{'one':{'name':'One'}, 'two':{'name':'Two'}},
                         'deliveries':{'a':{'state':'confirmed','allocations':[{'customer_id':'one','share':1}],
                                          'source':'evidence.md','evidence':'Confirmed'},
                                     'b':{'state':'unknown','allocations':[],'source':'','evidence':''}}}
        self.record = dict(work_id='a/task/BE', feature_id='a', delivery_key='a', role='BE', period='2026-Q3',
                           estimate_days=2, estimate_basis='task-estimate', actual_days=3,
                           actual_basis='explicit-confirmation', status='completed',developer='Developer',
                           source='evidence.md',evidence='Confirmed effort')
        self.ledger = dict(schema_version=1,as_of='2026-10-23',period_basis='completion-quarter',policy='Explicit work',
                           sources={'evidence.md':hashlib.sha256((self.root/'evidence.md').read_bytes()).hexdigest()},records=[self.record])

    def test_unknown_customer_and_unknown_fact_remain_visible(self):
        self.record.update(feature_id='b',delivery_key='b',actual_days=None,actual_basis='unknown')
        r=build_report(self.root,self.ledger,self.registry)['summary'][0]
        self.assertEqual(r['customer_id'],'unassigned');self.assertEqual(r['actual_unknown_items'],1)
        with self.assertRaises(ValueError):validate_registry(self.root,self.registry,require_confirmed=True)

    def test_shared_allocation_conserves_effort(self):
        self.registry['deliveries']['a']['allocations']=[{'customer_id':'one','share':'.25'},{'customer_id':'two','share':'.75'}]
        rows=build_report(self.root,self.ledger,self.registry)['summary']
        self.assertEqual(sum(r['known_actual_days'] for r in rows),3)
        self.assertEqual(sum(r['known_estimate_days'] for r in rows),2)

    def test_posthoc_is_not_initial_estimate(self):
        self.record['estimate_basis']='posthoc'
        r=build_report(self.root,self.ledger,self.registry)['summary'][0]
        self.assertEqual(r['known_estimate_days'],0);self.assertEqual(r['posthoc_days'],2)

    def test_unestimated_completed_work_keeps_actual(self):
        self.record.update(estimate_days=None,estimate_basis='unknown')
        r=build_report(self.root,self.ledger,self.registry)['summary'][0]
        self.assertEqual(r['known_actual_days'],3);self.assertEqual(r['estimate_unknown_items'],1)

    def test_cancellation_keeps_sunk_actual_effort(self):
        self.record['status']='cancelled'
        r=build_report(self.root,self.ledger,self.registry)['summary'][0]
        self.assertEqual(r['known_actual_days'],3);self.assertEqual(r['known_estimate_days'],0)

    def test_duplicate_work_rejected(self):
        self.ledger['records'].append(deepcopy(self.record))
        with self.assertRaises(ValueError):build_report(self.root,self.ledger,self.registry)

    def test_duplicate_estimate_across_periods_rejected(self):
        other=deepcopy(self.record);other['period']='2026-Q4';self.ledger['records'].append(other)
        with self.assertRaises(ValueError):build_report(self.root,self.ledger,self.registry)

    def test_invalid_numbers_rejected(self):
        for value in [float('nan'),float('inf'),-1,True]:
            with self.subTest(value=value):
                self.record['actual_days']=value
                with self.assertRaises(ValueError):build_report(self.root,self.ledger,self.registry)

    def test_stale_evidence_rejected(self):
        (self.root/'evidence.md').write_text('Changed')
        with self.assertRaises(ValueError):build_report(self.root,self.ledger,self.registry)

    def test_unsafe_source_rejected(self):
        self.registry['deliveries']['a']['source']='../evidence.md'
        with self.assertRaises(ValueError):validate_registry(self.root,self.registry)

    def test_missing_feature_and_invalid_shares_rejected(self):
        bad=deepcopy(self.registry);del bad['deliveries']['b']
        with self.assertRaises(ValueError):validate_registry(self.root,bad)
        self.registry['deliveries']['a']['allocations'][0]['share']=.5
        with self.assertRaises(ValueError):validate_registry(self.root,self.registry)

    def test_scope_rejects_customer_rename_affecting_other_feature(self):
        after=deepcopy(self.registry);after['customers']['one']['name']='Renamed'
        with self.assertRaises(ValueError):check_registry_scope(self.root,self.registry,after,{'features':['b']})
        check_registry_scope(self.root,self.registry,after,{'features':['a']})

    def test_reports_are_quarter_scoped_without_plan_permissions(self):
        scope={'features':['a'],'quarters':['2026-Q3']}
        self.assertEqual(path_kind('planning/2026-Q3/reports/review.md',scope),'execution-analysis')
        self.assertIsNone(path_kind('planning/2026-Q4/reports/review.md',scope))
        self.assertIsNone(path_kind('planning/2026-Q3/quarter/quarter-plan.md',scope))

    def test_historical_and_current_delivery_customers_stay_distinct(self):
        (self.root/'delivery-index.json').write_text(json.dumps({'deliveries':{'a':{'feature_id':'a'},'a-old':{'feature_id':'a'},'b':{'feature_id':'b'}}}))
        historical=deepcopy(self.registry['deliveries']['a'])
        historical['allocations']=[{'customer_id':'two','share':1}]
        self.registry['deliveries']['a-old']=historical
        self.record['delivery_key']='a-old'
        report=build_report(self.root,self.ledger,self.registry)
        self.assertEqual(report['summary'][0]['customer_id'],'two')
        self.assertEqual(report['delivery_customers']['a-old'],historical)
        before=deepcopy(self.registry);self.registry['deliveries']['a-old']['evidence']='New confirmation'
        with self.assertRaises(ValueError):check_registry_scope(self.root,before,self.registry,{'features':['a']})
        check_registry_scope(self.root,before,self.registry,{'features':['a-old']})

    def test_unknown_delivery_override_is_rejected(self):
        self.registry['deliveries']['missing']=deepcopy(self.registry['deliveries']['a'])
        with self.assertRaises(ValueError):validate_registry(self.root,self.registry)

    def test_delivery_identity_checked(self):
        (self.root/'delivery-index.json').write_text(json.dumps({'deliveries':{'a':{'feature_id':'a'},'b':{'feature_id':'b'}}}))
        self.record['delivery_key']='b'
        with self.assertRaises(ValueError):build_report(self.root,self.ledger,self.registry)

    def test_duplicate_json_keys_rejected(self):
        p=self.root/'duplicate.json';p.write_text('{"customers":{},"customers":{}}')
        with self.assertRaises(ValueError):read_json(p)


if __name__=='__main__':unittest.main()
