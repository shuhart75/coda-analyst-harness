from __future__ import annotations

from tracker_history import pagination_window, pin_history_response, pointer, validate_call


def confirm_membership(run_id: str, provider: str, release: str, keys: set[str], confirmation: dict) -> dict:
    from tracker_workflow import digest_bytes, normalize_key, response_file

    if (not isinstance(confirmation, dict) or confirmation.get('analyst_confirmed') is not True
            or confirmation.get('complete') is not True
            or confirmation.get('run_id') != run_id or confirmation.get('provider') != provider
            or confirmation.get('release') != release):
        raise ValueError('Explicit full membership confirmation must match this run, provider and release')
    members = confirmation.get('member_keys')
    if not isinstance(members, list) or not all(isinstance(key, str) for key in members):
        raise ValueError('Confirmed release membership requires an explicit key list')
    normalized = [normalize_key(key) for key in members]
    if len(set(normalized)) != len(normalized) or set(normalized) != keys:
        raise ValueError('Confirmed full membership must match every collected key, including skipped roles')
    source = confirmation.get('source', {})
    quote = source.get('quote')
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError('The actual analyst confirmation quote is required')
    path, raw, _ = response_file(source['file'], run_id, require_json=False)
    if digest_bytes(raw) != source.get('sha256') or quote not in raw.decode('utf-8'):
        raise ValueError('Analyst membership confirmation evidence changed or quote is missing')
    pin_history_response(run_id, path, raw, {'purpose': 'analyst-release-membership',
                                          'source_file': str(path), 'member_keys': sorted(normalized)})
    return {**confirmation, 'member_keys': sorted(normalized),
            'source': {**source, 'file': str(path)}}


def membership_evidence(run_id: str, provider: str, release: str, selection: str,
                        cards: list[dict], specification: dict) -> tuple[dict, list[str]]:
    from tracker_workflow import digest_bytes, normalize_key, response_file

    if not isinstance(specification, dict) or specification.get("schema_version") != 1:
        raise ValueError("Release membership requires an evidence manifest, not edited cards")
    sources = specification.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Release membership evidence sources are required")
    keys, evidence, windows, totals, continuations = set(), [], [], set(), []
    complete_metadata, explicitly_incomplete = True, False
    for source in sources:
        call = source["call"]
        validate_call(call, provider)
        if call.get("selection") != selection:
            raise ValueError("Membership call must select this release only")
        path, raw, payload = response_file(source["response_file"], run_id)
        if digest_bytes(raw) != source["sha256"]:
            raise ValueError("Release membership response checksum changed")
        mapping = source["mapping"]
        pin_history_response(run_id, path, raw, {"purpose": "release-membership", "call": call})
        records = pointer(payload, mapping["records"])
        if not isinstance(records, list):
            raise ValueError("Membership records must be an array")
        basis = source.get("basis")
        if basis not in {"selected-query", "links"}:
            raise ValueError("Membership basis must be selected-query or links")
        if basis == "links" and (not mapping.get("relation_value")
                                  or not ({"release_key", "release_root"} & set(mapping))):
            raise ValueError("Link evidence requires relation semantics and release endpoint paths")
        for record in records:
            if basis == "links":
                endpoint = (pointer(payload, mapping["release_root"]) if "release_root" in mapping
                            else pointer(record, mapping["release_key"]))
                if endpoint != release or pointer(record, mapping["relation"]) != mapping["relation_value"]:
                    continue
            key = normalize_key(pointer(record, mapping["member_key"]))
            if key in keys:
                raise ValueError("Repeated release member; inspect overlapping pages or links")
            keys.add(key)
        window = pagination_window(payload, mapping, call, len(records))
        if window is None:
            explicitly_incomplete |= (('has_next' in mapping and pointer(payload, mapping['has_next']) is True)
                                      or ('total' in mapping and pointer(payload, mapping['total']) > len(records)))
        if window is not None:
            start, end, total = window
            if total is not None:
                totals.add(total)
            windows.append((start, end))
            if "has_next" in mapping:
                continuations.append((end, pointer(payload, mapping["has_next"])))
        elif len(sources) == 1 and source.get("unpaginated_contract"):
            if "has_next" in mapping and pointer(payload, mapping["has_next"]):
                raise ValueError("Unpaginated contract contradicts the returned continuation flag")
            contract = source["unpaginated_contract"]
            contract_path, contract_raw, _ = response_file(contract["response_file"], run_id)
            if (digest_bytes(contract_raw) != contract["sha256"] or not contract.get("quote", "").strip()
                    or contract["quote"] not in contract_raw.decode("utf-8")
                    or call["capability_source"] != str(contract_path)):
                raise ValueError("Complete unpaginated response requires the recorded capability contract")
            pin_history_response(run_id, contract_path, contract_raw, {"purpose": "release-membership-capability"})
            totals.add(len(records))
            windows.append((0, len(records)))
        else:
            complete_metadata = False
        evidence.append({"response_file": str(path), "sha256": source["sha256"],
                         "call": call, "mapping": mapping, "basis": basis,
                         "unpaginated_contract": source.get("unpaginated_contract")})
    returned = {card["key"] for card in cards}
    if len(returned) != len(cards) or returned != keys:
        raise ValueError("Membership evidence and collected cards must cover the same unique keys")
    position, coverage_gap = 0, False
    for start, end in sorted(windows):
        if start != position:
            complete_metadata = False
            coverage_gap = True
        position = end
    complete = complete_metadata and totals == {position}
    if complete and any(has_next != (end < position) for end, has_next in continuations):
        raise ValueError("Page continuation contradicts complete membership coverage")
    confirmation = None
    if 'analyst_confirmation' in specification:
        if (explicitly_incomplete or coverage_gap or (totals and totals != {position})
                or any(has_next != (end < position) for end, has_next in continuations)):
            raise ValueError('Resolve explicit pagination gaps before confirming release membership')
        confirmation = confirm_membership(run_id, provider, release, keys, specification['analyst_confirmation'])
    limitations = [] if complete else [
        'release-membership:source-completeness-unproven:analyst-confirmed' if confirmation else
        'release-result-incomplete:membership-completeness-not-proven']
    return {"release": release, "keys": sorted(keys), "sources": evidence,
            "complete": complete or confirmation is not None, "source_complete": complete,
            "completeness_basis": 'source' if complete else 'analyst-confirmed' if confirmation else 'unproven',
            "analyst_confirmation": confirmation}, limitations


def review_membership(run_id: str, result: dict, specification: dict | None) -> dict:
    import copy
    from tracker_workflow import load_run

    if specification is None:
        return result
    run = load_run(run_id)
    provider, release = result["scope"]["provider"], result["scope"]["ids"][0]
    if result["scope"]["kind"] != "release" or run.get("collection_mode") != "adaptive":
        raise ValueError("Supplementary membership evidence requires an adaptive release run")
    step = next(step for step in run["steps"] if step["provider"] == provider)
    evidence, limitations = membership_evidence(run_id, provider, release, step["query"],
                                               run["cards"][provider], specification)
    reviewed = copy.deepcopy(result)
    reviewed["release_membership_evidence"] = evidence
    if evidence["complete"]:
        reviewed["limitations"] = [limit for limit in reviewed["limitations"]
                                  if limit != "release-result-incomplete:membership-completeness-not-proven"]
    reviewed["limitations"] = sorted(set([*reviewed["limitations"], *limitations]))
    return reviewed


def validate_membership(run: dict, step: dict, cards: list[dict]) -> None:
    release = run["scope"]["ids"][0]
    specification = step.get("call", {}).get("release_membership")
    evidence = None
    if specification is not None:
        evidence, limitations = membership_evidence(run["run_id"], step["provider"], release,
                                                   step["query"], cards, specification)
        run["limitations"].extend(limitations)
        step["release_membership"] = evidence
    for card in cards:
        if any(release in {item.get("key"), item.get("name")} for item in card["releases"]):
            continue
        if evidence is None:
            raise ValueError("Every release card requires membership in fields or separate release_membership evidence")
        if card["releases"]:
            raise ValueError("Card release fields contradict membership evidence; review the conflict")
        card["releases"] = [{"key": release}]
        card["release_membership_basis"] = "separate-evidence"
