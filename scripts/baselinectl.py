#!/usr/bin/env python3
"""Durable release-to-baseline review commands (no tracker reads)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import baseline_releases as baseline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    observe = commands.add_parser("observe")
    observe.add_argument("--observation", required=True, type=Path)
    run = commands.add_parser("observe-run")
    run.add_argument("--run-id", required=True)
    run.add_argument("--review", required=True, type=Path)
    commands.add_parser("scan")
    hashes = commands.add_parser("review-hashes")
    hashes.add_argument("--candidate", required=True, type=Path)
    defer = commands.add_parser("defer")
    defer.add_argument("--release", required=True)
    defer.add_argument("--reason", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--release", required=True)
    prepare.add_argument("--candidate", required=True, type=Path)
    prepare.add_argument("--review", required=True, type=Path)
    promote = commands.add_parser("promote")
    promote.add_argument("--release", required=True)
    promote.add_argument("--scope-hash", required=True)
    promote.add_argument("--deployment", required=True, type=Path)
    promote.add_argument("--analyst-confirmed", action="store_true")
    reconciliation = commands.add_parser("prepare-reconciliation")
    reconciliation.add_argument("--documentation-version", required=True)
    reconciliation.add_argument("--candidate", required=True, type=Path)
    reconciliation.add_argument("--review", required=True, type=Path)
    reconciliation_promote = commands.add_parser("promote-reconciliation")
    reconciliation_promote.add_argument("--documentation-version", required=True)
    reconciliation_promote.add_argument("--review-hash", required=True)
    reconciliation_promote.add_argument("--analyst-confirmed", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "observe":
            result = baseline.observe(args.project, json.loads(args.observation.read_text()))
        elif args.command == "observe-run":
            result = baseline.observe_run(args.project, args.run_id, args.review)
        elif args.command == "scan":
            result = baseline.scan(args.project)
        elif args.command == "review-hashes":
            result = baseline.review_hashes(args.candidate)
        elif args.command == "defer":
            result = baseline.defer(args.project, args.release, args.reason)
        elif args.command == "prepare":
            result = baseline.prepare(args.project, args.release, args.candidate, json.loads(args.review.read_text()))
        elif args.command == "prepare-reconciliation":
            result = baseline.prepare_reconciliation(args.project, args.documentation_version, args.candidate,
                                                       json.loads(args.review.read_text()))
        elif args.command == "promote-reconciliation":
            result = baseline.promote_reconciliation(args.project, args.documentation_version, args.review_hash,
                                                       args.analyst_confirmed)
        else:
            result = baseline.promote(args.project, args.release, args.scope_hash, json.loads(args.deployment.read_text()), args.analyst_confirmed)
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
