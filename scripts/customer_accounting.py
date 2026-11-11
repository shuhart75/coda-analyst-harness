"""Customer ownership and evidence-bound effort accounting; no tracker or file writes."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

REGISTRY = 'planning/customers.json'


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'Duplicate JSON key: {key}')
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=pairs)


def source_path(project, relative):
    if not isinstance(relative, str) or not relative:
        raise ValueError('Evidence requires a project-relative source')
    path = Path(relative)
    target = project / path
    if path.is_absolute() or '..' in path.parts or target.resolve() != target.absolute() or not target.is_file():
        raise ValueError(f'Unsafe or missing source: {relative}')
    return target


def number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError('Effort and allocation must be finite numbers')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Invalid number') from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise ValueError('Effort must be nonnegative; allocation must be positive')
    return result


def feature_ids(project):
    index = project / 'delivery-index.json'
    if index.exists():
        return {d['feature_id'] for d in read_json(index)['deliveries'].values()}
    return {p.name for p in (project / 'features').iterdir() if p.is_dir()}


def delivery_ids(project):
    index = project / 'delivery-index.json'
    return set(read_json(index)['deliveries']) if index.exists() else feature_ids(project)


def validate_registry(project, data=None, *, require_confirmed=False):
    project = Path(project).resolve()
    data = read_json(project / REGISTRY) if data is None else data
    if set(data) != {'schema_version', 'customers', 'deliveries'} or type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise ValueError('Unsupported customer registry schema')
    customers, deliveries = data['customers'], data['deliveries']
    if not isinstance(customers, dict) or not isinstance(deliveries, dict):
        raise ValueError('Customers and deliveries must be objects')
    if set(deliveries) != delivery_ids(project):
        raise ValueError('Customer registry must cover every delivery exactly once')
    for key, value in customers.items():
        if not re.fullmatch(r'[a-z][a-z0-9-]*', key) or key == 'unassigned':
            raise ValueError('Invalid or reserved customer id')
        if not isinstance(value, dict) or set(value) != {'name'} or not isinstance(value['name'], str) or not value['name'].strip():
            raise ValueError('Customer requires a display name')
    unknown = []
    for delivery, binding in deliveries.items():
        if not isinstance(binding, dict) or set(binding) != {'state', 'allocations', 'source', 'evidence'}:
            raise ValueError(f'Invalid customer binding: {delivery}')
        if binding['state'] == 'unknown':
            if binding['allocations'] != [] or binding['source'] != '' or binding['evidence'] != '':
                raise ValueError('Unknown customer cannot have an allocation or approval')
            unknown.append(delivery)
            continue
        if binding['state'] != 'confirmed' or not isinstance(binding['evidence'], str) or not binding['evidence'].strip():
            raise ValueError('Customer allocation requires analyst evidence')
        source_path(project, binding['source'])
        allocations = binding['allocations']
        if not isinstance(allocations, list) or not allocations:
            raise ValueError('Confirmed customer requires allocations')
        seen, total = set(), Decimal(0)
        for item in allocations:
            if not isinstance(item, dict) or set(item) != {'customer_id', 'share'} or item['customer_id'] not in customers or item['customer_id'] in seen:
                raise ValueError('Unknown or duplicate customer allocation')
            seen.add(item['customer_id'])
            total += number(item['share'], positive=True)
        if total != 1:
            raise ValueError('Customer shares must sum exactly to one')
    if require_confirmed and unknown:
        raise ValueError('Unconfirmed customers: ' + ', '.join(sorted(unknown)))
    return data


def check_registry_scope(project, before, after, scope):
    """A customer belongs to a delivery, including historical deliveries."""
    validate_registry(project, after)
    old = before.get('deliveries', {})
    changed = {k for k in set(old) | set(after['deliveries']) if old.get(k) != after['deliveries'].get(k)}
    changed_customers = {k for k in set(before.get('customers', {})) | set(after['customers'])
                         if before.get('customers', {}).get(k) != after['customers'].get(k)}
    for k, binding in after['deliveries'].items():
        if any(a['customer_id'] in changed_customers for a in binding['allocations']):
            changed.add(k)
    if not changed <= set(scope['features']):
        raise ValueError('Customer change outside confirmed execution scope: ' + ', '.join(sorted(changed - set(scope['features']))))


def build_report(project, ledger, registry=None):
    project = Path(project).resolve()
    registry = validate_registry(project, registry)
    if set(ledger) != {'schema_version', 'as_of', 'period_basis', 'policy', 'sources', 'records'} or ledger['schema_version'] != 1:
        raise ValueError('Unsupported effort ledger schema')
    if ledger['period_basis'] not in {'completion-quarter', 'delivery-quarter', 'work-date'}:
        raise ValueError('Declare the period accounting basis')
    if not ledger['policy'] or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', ledger['as_of']):
        raise ValueError('Declare accounting policy and snapshot date')
    sources = ledger['sources']
    for relative, digest in sources.items():
        actual = hashlib.sha256(source_path(project, relative).read_bytes()).hexdigest()
        if actual != digest:
            raise ValueError(f'Stale effort evidence: {relative}')
    deliveries = read_json(project / 'delivery-index.json')['deliveries'] if (project / 'delivery-index.json').exists() else None
    seen, work_bindings, records = set(), {}, []
    fields = {'work_id', 'feature_id', 'delivery_key', 'role', 'period', 'estimate_days', 'estimate_basis',
              'actual_days', 'actual_basis', 'status', 'developer', 'source', 'evidence'}
    for row in ledger['records']:
        if set(row) != fields:
            raise ValueError('Invalid effort record fields')
        feature = row['feature_id']
        if feature not in feature_ids(project) or row['role'] not in {'AN', 'BE', 'FE', 'QA'}:
            raise ValueError('Unknown feature or role')
        if row['delivery_key'] not in registry['deliveries']:
            raise ValueError('Unknown customer delivery')
        if deliveries is None and row['delivery_key'] != feature:
            raise ValueError('Legacy delivery must match its feature')
        if deliveries is not None and (row['delivery_key'] not in deliveries or deliveries[row['delivery_key']]['feature_id'] != feature):
            raise ValueError('Delivery does not belong to the declared feature')
        if not isinstance(row['work_id'], str) or not row['work_id'] or not re.fullmatch(r'\d{4}-Q[1-4]|unknown', row['period']):
            raise ValueError('Stable work identity and explicit period are required')
        key = (row['work_id'], row['period'])
        if key in seen:
            raise ValueError('Duplicate work item in the same period')
        seen.add(key)
        owner = (feature, row['role'], row['delivery_key'])
        if row['work_id'] in work_bindings and work_bindings[row['work_id']] != owner:
            raise ValueError('A work item cannot change feature, delivery or role between periods')
        work_bindings[row['work_id']] = owner
        if row['source'] not in sources or not isinstance(row['evidence'], str) or not row['evidence'].strip():
            raise ValueError('Every effort record requires hashed source evidence')
        if row['status'] not in {'completed', 'in-progress', 'not-started', 'unknown', 'cancelled'}:
            raise ValueError('Invalid work status')
        if row['estimate_basis'] not in {'task-estimate', 'posthoc', 'unknown'}:
            raise ValueError('Invalid estimate basis')
        if row['actual_basis'] not in {'explicit-confirmation', 'recorded-working-days', 'unknown'}:
            raise ValueError('Invalid actual basis')
        for field, basis in [('estimate_days', 'estimate_basis'), ('actual_days', 'actual_basis')]:
            value = row[field]
            if (value is None) != (row[basis] == 'unknown'):
                raise ValueError('Unknown effort must be null, never an implicit zero')
            if value is not None:
                number(value)
        if row['developer'] is not None and (row['role'] not in {'BE', 'FE'} or not isinstance(row['developer'], str) or not row['developer'].strip()):
            raise ValueError('Developer identity applies only to development tasks')
        records.append(row)
    # A split interval may carry estimate only once; actuals must be disjoint per policy.
    estimated = set()
    for row in records:
        if row['estimate_days'] is not None:
            if row['work_id'] in estimated:
                raise ValueError('Estimate counted more than once across periods')
            estimated.add(row['work_id'])
    buckets = {}
    for row in records:
        if row['status'] == 'cancelled' and row['actual_days'] is None:
            continue
        binding = registry['deliveries'][row['delivery_key']]
        allocations = binding['allocations'] or [{'customer_id': 'unassigned', 'share': 1}]
        for allocation in allocations:
            customer = allocation['customer_id']
            key = (row['period'], customer, row['role'])
            bucket = buckets.setdefault(key, dict(known_estimate_days=Decimal(0), known_actual_days=Decimal(0),
                                                  posthoc_days=Decimal(0), work_items=0, actual_unknown_items=0, estimate_unknown_items=0))
            share = number(allocation['share'], positive=True)
            bucket['work_items'] += 1
            if row['status'] == 'cancelled':
                pass  # Cancellation removes current scope, not already spent effort.
            elif row['estimate_days'] is None:
                bucket['estimate_unknown_items'] += 1
            else:
                metric = 'posthoc_days' if row['estimate_basis'] == 'posthoc' else 'known_estimate_days'
                bucket[metric] += number(row['estimate_days']) * share
            if row['actual_days'] is None:
                bucket['actual_unknown_items'] += 1
            else:
                bucket['known_actual_days'] += number(row['actual_days']) * share
    summary = []
    for (period, customer, role), metrics in sorted(buckets.items()):
        summary.append(dict(period=period, customer_id=customer,
                            customer_name=registry['customers'][customer]['name'] if customer != 'unassigned' else 'Заказчик не указан',
                            role=role, **{k:float(v) if isinstance(v, Decimal) else v for k,v in metrics.items()}))
    return dict(schema_version=1, as_of=ledger['as_of'], period_basis=ledger['period_basis'], policy=ledger['policy'],
                registry_sha256=hashlib.sha256(json.dumps(registry, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                delivery_customers=registry['deliveries'], summary=summary, record_count=len(records),
                caveat='Known actual effort is not the total while actual_unknown_items is nonzero; posthoc values are not initial estimates.')
