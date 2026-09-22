from __future__ import annotations

from tracker_history import pin_history_response, pointer, validate_call


def membership_evidence(run_id: str, provider: str, release: str, selection: str,
                        cards: list[dict], specification: dict) -> tuple[dict, list[str]]:
    from tracker_workflow import digest_bytes, normalize_key, response_file

    if not isinstance(specification, dict) or specification.get("schema_version") != 1:
        raise ValueError("Release membership requires an evidence manifest, not edited cards")
    sources = specification.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Release membership evidence sources are required")
    keys, evidence, windows, totals = set(), [], [], set()
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
        if "total" in mapping and "start" in mapping:
            total, start = pointer(payload, mapping["total"]), pointer(payload, mapping["start"])
            if type(total) is not int or type(start) is not int or start < 0 or start + len(records) > total:
                raise ValueError("Invalid release membership pagination")
            totals.add(total)
            windows.append((start, start + len(records)))
        elif len(sources) == 1 and source.get("unpaginated_contract"):
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
    return {"release": release, "keys": sorted(keys), "sources": evidence, "complete": complete}, (
        [] if complete else ["release-result-incomplete:membership-completeness-not-proven"])


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
