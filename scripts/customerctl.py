#!/usr/bin/env python3
"""Validate customer ownership or report an explicitly reviewed effort ledger."""
import argparse
import json
from pathlib import Path
from customer_accounting import validate_registry, read_json, build_report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    commands = parser.add_subparsers(dest='action', required=True)
    validate = commands.add_parser('validate')
    validate.add_argument('--require-confirmed', action='store_true')
    report = commands.add_parser('report')
    report.add_argument('--ledger', required=True)
    args = parser.parse_args()
    try:
        project = Path(args.project).resolve()
        if args.action == 'report':
            result = build_report(project, read_json(args.ledger))
        else:
            data = validate_registry(project, require_confirmed=args.require_confirmed)
            result = {'status':'valid', 'deliveries':len(data['deliveries']),
                      'unknown_customers':[k for k,v in data['deliveries'].items() if v['state']=='unknown']}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({'status':'invalid', 'error':str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
