#!/usr/bin/env python3
"""Capture selected ratified Imprint judgments and checked Atlas claims.

Run with the installed Imprint environment. This uses its current read adapter,
never initializes/migrates the store, changes ratification or starts a producer.
The private packet is an observation of current state, not a historical snapshot.
"""
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile

SCHEMA = "fully-aware-execution-context/v1"


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def validate_policy(value):
    if not isinstance(value, dict) or set(value) != {"schema", "client_id", "project_id", "imprint_domain", "imprint_record_ids", "atlas_claim_ids"}:
        raise ValueError("context-policy-fields")
    if value["schema"] != "fully-aware-context-policy/v1":
        raise ValueError("context-policy-schema")
    for key in ("client_id", "project_id"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", value[key]):
            raise ValueError("context-policy-identity")
    if value["imprint_domain"] is not None and (not isinstance(value["imprint_domain"], str) or len(value["imprint_domain"]) > 160):
        raise ValueError("context-domain-invalid")
    for key in ("imprint_record_ids", "atlas_claim_ids"):
        values = value[key]
        if (not isinstance(values, list) or len(values) > 20 or
                any(not isinstance(x, str) or not re.fullmatch(r"[a-zA-Z0-9:_-]{1,160}", x) for x in values)
                or len(set(values)) != len(values)):
            raise ValueError("context-selection-invalid")
    return value


def capture_imprint(policy):
    from imprint.config import load_config, resolved_operator_root
    from imprint.store import ImprintStore
    from imprint.retrieve.store_source import StoreRetrievalSource
    from imprint.retrieve.engine import eligible
    records = StoreRetrievalSource(ImprintStore(resolved_operator_root(load_config()) / "imprint.db")).retrieval_candidates("current-observation")
    ratified = [r for r in records if r.provenance_status == "ratified" and r.authority_tier == "ratified_knowledge"
                and r.ontology_partition == "judgment" and eligible(r, policy["imprint_domain"])]
    selected = []
    for ident in policy["imprint_record_ids"]:
        matches = [r for r in ratified if r.record_id == ident]
        if len(matches) != 1:
            raise ValueError("selected-judgment-not-ratified-or-eligible")
        r = matches[0]
        if len(r.text.encode()) > 8000:
            raise ValueError("selected-judgment-too-large")
        selected.append({"record_id": r.record_id, "domain_id": r.domain_id,
            "authority": "operator-ratified", "text": r.text, "evidence_ids": list(r.evidence_ids),
            "valid_from": r.valid_from})
    return {"status": "selected" if selected else "no-ratified-judgments-selected",
            "eligible_ratified_count": len(ratified), "judgments": selected,
            "observation_sha256": sha(canonical(selected)), "historical_snapshot": False}


def capture_atlas(policy, module_path):
    path = Path(module_path)
    if not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError("atlas-module-path-invalid")
    spec = importlib.util.spec_from_file_location("fully_aware_atlas_capture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    claims = []
    for ident in policy["atlas_claim_ids"]:
        result = module.handle_check_claim({"claim_id": ident})
        if result.get("status") != "ok" or result.get("found") is not True:
            claims.append({"claim_id": ident, "status": "unavailable", "authority": "none"})
            continue
        claim = result.get("claim", {})
        if claim.get("claim_id") != ident:
            raise ValueError("atlas-claim-mismatch")
        verdict = result.get("verdict")
        # Atlas alone supplies the oracle decision. No LLM reclassification.
        if verdict not in {"current", "stale", "oracle-error", "not-mechanically-verifiable"}:
            verdict = "unavailable"
        fields = {k: claim.get(k) for k in ("claim_type", "subject", "state", "source_path", "source_commit", "line", "as_of", "extracted_at")}
        if len(canonical(fields)) > 8000:
            raise ValueError("atlas-claim-too-large")
        claims.append({"claim_id": ident, "status": verdict,
            "authority": "mechanically-verified" if verdict == "current" else "unverified-context",
            "source": fields, "routed_to": "STRATEGIC_REVIEW" if verdict == "not-mechanically-verifiable" else None})
    return {"status": "checked" if claims else "no-claims-selected", "claims": claims,
            "observation_sha256": sha(canonical(claims)), "module_sha256": sha(path.read_bytes())}


def capture(policy, atlas_path, captured_at, imprint_reader=capture_imprint, atlas_reader=capture_atlas):
    validate_policy(policy)
    if not isinstance(captured_at, dt.datetime) or captured_at.utcoffset() is None:
        raise ValueError("context-time-invalid")
    captured_at = captured_at.astimezone(dt.timezone.utc)
    limitations = []
    try:
        imprint = imprint_reader(policy)
    except (ImportError, OSError, ValueError, TypeError, KeyError):
        imprint = {"status": "unavailable", "eligible_ratified_count": None, "judgments": [],
                   "observation_sha256": None, "historical_snapshot": False}
        limitations.append("imprint-retrieval-unavailable")
    if not imprint["judgments"]:
        limitations.append("no-ratified-judgments-in-packet")
    try:
        atlas = atlas_reader(policy, atlas_path)
    except (ImportError, OSError, ValueError, TypeError, KeyError):
        atlas = {"status": "unavailable", "claims": [], "observation_sha256": None, "module_sha256": None}
        limitations.append("atlas-check-unavailable")
    if not atlas["claims"]:
        limitations.append("no-atlas-claims-in-packet")
    if any(c["status"] != "current" for c in atlas["claims"]):
        limitations.append("atlas-claims-need-review")
    return {"schema": SCHEMA, "client_id": policy["client_id"], "project_id": policy["project_id"],
        "captured_at": captured_at.isoformat(), "expires_at": (captured_at + dt.timedelta(hours=24)).isoformat(),
        "policy_sha256": sha(canonical(policy)), "permitted_effect": "local-draft",
        "grants_action_authority": False, "imprint": imprint, "atlas": atlas, "limitations": limitations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--atlas-module", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    policy = json.loads(Path(args.policy).read_bytes())
    packet = capture(policy, args.atlas_module, dt.datetime.now(dt.timezone.utc))
    raw = canonical(packet)
    if len(raw) > 64_000:
        raise ValueError("context-packet-too-large")
    target = Path(args.out).absolute()
    if any(p.is_symlink() for p in [target, *target.parents]):
        raise ValueError("context-output-symlink")
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".context-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        # Exclusive publication: a workflow's selected packet is immutable.
        os.link(name, target)
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        os.unlink(name)
    print(json.dumps({"schema": SCHEMA, "sha256": sha(raw), "captured_at": packet["captured_at"],
        "judgment_count": len(packet["imprint"]["judgments"]), "claim_count": len(packet["atlas"]["claims"]),
        "limitations": packet["limitations"]}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError):
        print('{"error":"context-capture-failed"}', file=sys.stderr)
        sys.exit(1)
