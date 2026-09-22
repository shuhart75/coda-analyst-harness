from __future__ import annotations

from tracker_history import pagination_window, pin_history_response, pointer, validate_call


def membership_evidence(run_id: str, provider: str, release: str, selection: str,
                        cards: list[dict], specification: dict) -> tuple[dict, list[str]]:
    from tracker_workflow import digest_bytes, normalize_key, response_file

    if not isinstance(specification, dict) or specification.get("schema_version") != 1:
        raise ValueError("Release membership requires an evidence manifest, not edited cards")
    sources = specification.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Release membership evidence sources are required")
    keys, evidence, windows, totals, continuations = set(), [], [], set(), []
    complete_metadata = True
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
    position = 0
    for start, end in sorted(windows):
        if start != position:
            complete_metadata = False
        position = end
    complete = complete_metadata and totals == {position}
    if complete and any(has_next != (end < position) for end, has_next in continuations):
        raise ValueError("Page continuation contradicts complete membership coverage")
    return {"release": release, "keys": sorted(keys), "sources": evidence, "complete": complete}, (
        [] if complete else ["release-result-incomplete:membership-completeness-not-proven"])


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
    else:
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
