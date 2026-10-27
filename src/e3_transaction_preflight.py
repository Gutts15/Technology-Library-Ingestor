#!/usr/bin/env python3
"""E3 offline transaction preflight and in-memory canonical simulation.

Never writes canonical, inbox, lot, decision, index or receipt bytes. The
existing transaction engine is exercised on a temporary in-memory overlay.
A production coordinator, durable journal and recovery remain separate gates.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

from candidate_canonical_transaction_engine import execute_canonical_transaction
from candidate_canonical_index_rebuild import build_index_plan
from candidate_validator import normalize_title, parse_master_index
from candidate_transaction_plan import TRANSACTION_PLAN_VERSION
from e1_lote_prepare import LOTES, Storage
from e2_gemini_decide import DecisionError, lot_digest, strict_json, validate_lote, validate_model_output
from library_index_build import local_record_paths, validate_record

MAX_DECISIONS_BYTES = 400 * 1024
MAX_LOTE_BYTES = 200 * 1024
PATH_PREFIX = "99_INBOX/CANDIDATES/E3_PREPARED/"
INDEX_PATH = "00_LIBRARY/MASTER_INDEX.md"
RECORD_TYPES = {"TECHNOLOGY", "PATTERN", "PIPELINE", "SOURCE"}
SAFE_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class PreflightError(Exception):
    pass


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def slugify(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title)
    simplified = "".join(ch for ch in ascii_title if not unicodedata.combining(ch))
    result = re.sub(r"[^a-z0-9]+", "-", simplified.casefold()).strip("-")
    if not SAFE_SLUG.fullmatch(result) or len(result) > 100:
        raise PreflightError("slug_invalid")
    return result


def canonical_path(record: dict) -> str:
    slug = slugify(record["title"])
    name = record["record_type"].lower() + "-" + slug + ".md"
    if record["record_type"] == "SOURCE":
        return "00_LIBRARY/SOURCES/" + name
    return "00_LIBRARY/" + record["domain"] + "/" + record["category"] + "/" + name


def append_verified_claims(original: bytes, claims: list[dict]) -> bytes:
    """Append only validated E2 claim text to original canonical bytes.

    Model-authored UPDATE Markdown supplies a header/route proposal but must
    never replace existing canonical prose or STATUS. E3 owns the merge.
    """
    if not isinstance(claims, list) or not 1 <= len(claims) <= 12:
        raise PreflightError("update_claims_invalid")
    try:
        prior = original.decode("utf-8")
    except UnicodeError:
        raise PreflightError("update_original_invalid") from None
    if "\x00" in prior or len(original) > 128 * 1024:
        raise PreflightError("update_original_invalid")
    additions = []
    seen = set()
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {"text", "evidence_quote"}:
            raise PreflightError("update_claims_invalid")
        claim_text = claim.get("text")
        evidence = claim.get("evidence_quote")
        if not isinstance(claim_text, str) or not isinstance(evidence, str):
            raise PreflightError("update_claims_invalid")
        # Make untrusted content inert as a single-line Markdown data field.
        claim_text = " ".join(claim_text.split())
        evidence = " ".join(evidence.split())
        if (not 8 <= len(evidence) <= 600 or not 4 <= len(claim_text) <= 1200
                or any(ch in claim_text + evidence for ch in "<>[]`\\\\")
                or any(ord(ch) < 32 for ch in claim_text + evidence)):
            raise PreflightError("update_claim_unsafe")
        key = normalize_title(claim_text)
        if key in seen:
            raise PreflightError("update_duplicate_claim")
        seen.add(key)
        if key in normalize_title(prior):
            raise PreflightError("update_claim_already_present")
        additions.append(f"- {claim_text} (Evidence excerpt: \"{evidence}\")")
    joined = prior.rstrip("\n") + "\n\n## EVIDENCE ADDITIONS\n" + "\n".join(additions) + "\n"
    output = joined.encode("utf-8")
    if len(output) > 128 * 1024:
        raise PreflightError("update_result_oversize")
    return output


def nonwrite_decision_is_safe(decision: dict) -> None:
    if decision["acao"] not in {"HOLD", "REJECT", "UNCHANGED"}:
        raise PreflightError("unexpected_action")


def compile_transaction(root: Path, raw_lote: bytes, lot_relative: str,
                        raw_decisions: bytes) -> tuple[dict, dict[str, bytes], list[str], dict]:
    try:
        lote = validate_lote(raw_lote, lot_relative)
        decisions_file = strict_json(raw_decisions)
        if (not isinstance(decisions_file, dict)
                or set(decisions_file) != {"schema_version", "lote_id", "lote_sha256",
                                           "model", "decisoes", "canonical_write_performed"}
                or decisions_file["schema_version"] != 1
                or decisions_file["lote_id"] != lote["lote_id"]
                or decisions_file["lote_sha256"] != checksum(raw_lote)
                or decisions_file["model"] != "gemini-3.5-flash-lite"
                or decisions_file["canonical_write_performed"] is not False):
            raise PreflightError("decisions_binding_invalid")
        decisions = validate_model_output(lote, {"decisoes": decisions_file["decisoes"]})
    except DecisionError:
        raise PreflightError("decisions_invalid") from None
    store = Storage(root)
    master = store.read(INDEX_PATH, 128 * 1024)
    try:
        master_text = master.decode("utf-8")
    except UnicodeError:
        raise PreflightError("master_invalid") from None
    paths = local_record_paths(root)
    if not paths:
        raise PreflightError("canonical_inventory_missing")
    baseline_indexes, records, errors = build_index_plan(paths, lambda p: (root / p).read_bytes() if (root / p).is_file() else None)
    if errors or baseline_indexes is None or baseline_indexes.get(INDEX_PATH) != master:
        raise PreflightError("canonical_index_stale")
    # The canonical index is an authority for record IDs and paths, not a
    # justification to create a name colliding with existing knowledge.
    indexed = parse_master_index(master_text)
    if len(indexed) != len(records):
        raise PreflightError("canonical_index_mismatch")
    title_keys = {(r["type"], r["title_key"]) for r in indexed}
    record_ids: dict[str, str] = {r["record_id"]: r["path"] for r in records}
    overlay: dict[str, bytes] = {}
    staged: dict[str, bytes] = {}
    planned = []
    settled = []
    targets: set[str] = set()
    for item, decision in zip(lote["itens"], decisions):
        action = decision["acao"]
        outcome = {"item_id": item["item_id"], "acao": action, "record_id": None}
        if action not in {"CREATE", "UPDATE"}:
            nonwrite_decision_is_safe(decision)
            settled.append(outcome)
            continue
        if decision["record_type"] not in RECORD_TYPES:
            raise PreflightError("unsupported_canonical_type")
        raw = decision["record_markdown"].encode("utf-8")
        if len(raw) > 128 * 1024 or b"\x00" in raw:
            raise PreflightError("record_payload_invalid")
        header = {}
        for line in raw.decode("utf-8").splitlines()[:40]:
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip().upper() in {"RECORD_ID", "TYPE", "STATUS", "DOMAIN", "CATEGORY", "TITLE"}:
                    header[k.strip().upper()] = v.strip()
        required = {"RECORD_ID", "TYPE", "STATUS", "DOMAIN", "CATEGORY", "TITLE"}
        if not required.issubset(header):
            raise PreflightError("record_header_missing")
        if (header["TYPE"] != decision["record_type"]
                or header["STATUS"] != "TEST"
                or header["DOMAIN"] + "/" + header["CATEGORY"] != decision["route"]):
            raise PreflightError("record_header_mismatch")
        target = canonical_path({"record_type": header["TYPE"],
                                 "domain": header["DOMAIN"],
                                 "category": header["CATEGORY"],
                                 "title": header["TITLE"]})
        if target in targets:
            raise PreflightError("batch_target_collision")
        targets.add(target)
        try:
            validate_record(target, raw)
        except ValueError:
            raise PreflightError("canonical_record_invalid") from None
        if action == "CREATE":
            record_id = header["RECORD_ID"]
            if (record_id in record_ids
                    or (header["TYPE"], normalize_title(header["TITLE"])) in title_keys
                    or (root / target).exists()):
                raise PreflightError("create_canonical_collision")
            base_sha = None
            condition = "TARGET_ABSENT"
            title_keys.add((header["TYPE"], normalize_title(header["TITLE"])))
            record_ids[record_id] = target
        else:
            record_id = decision["target_record_id"]
            if header["RECORD_ID"] != record_id or record_ids.get(record_id) != target:
                raise PreflightError("update_record_identity")
            original = store.read(target, 128 * 1024)
            try:
                current = validate_record(target, original)
            except ValueError:
                raise PreflightError("update_existing_record_invalid") from None
            if (current["record_id"] != record_id
                    or current["type"] != header["TYPE"]
                    or current["domain"] != header["DOMAIN"]
                    or current["category"] != header["CATEGORY"]
                    or normalize_title(current["title"]) != normalize_title(header["TITLE"])):
                raise PreflightError("update_header_drift")
            # Discard model-written replacement prose. Merge only validated,
            # quote-grounded claims beneath the original canonical bytes.
            raw = append_verified_claims(original, decision["claims"])
            try:
                validate_record(target, raw)
            except ValueError:
                raise PreflightError("update_merged_record_invalid") from None
            base_sha = checksum(original)
            condition = "EXACT_BASE_SHA"
        content_path = PATH_PREFIX + item["item_id"] + ".md"
        staged[content_path] = raw
        planned.append({"lane": "NEW" if action == "CREATE" else "UPDATE",
                        "action": action, "candidate_id": item["item_id"],
                        "package_id": item["package_id"],
                        "revision_key": item["revision_key"],
                        "record_id": record_id, "target_path": target,
                        "content_path": content_path, "content_sha256": checksum(raw),
                        "base_sha256": base_sha, "precondition": condition})
        settled.append({**outcome, "record_id": record_id})
    tx_id = checksum(raw_lote + raw_decisions + master)[:20]
    plan = {"schema_version": 1, "transaction_plan_version": TRANSACTION_PLAN_VERSION,
            "transaction_id": tx_id, "master_index_sha256": checksum(master),
            "canonical_write_performed": False, "requires_live_revalidation": True,
            "requires_atomic_record_writes": True,
            "requires_index_rebuild_after_write": bool(planned),
            "requires_byte_verification_after_write": bool(planned),
            "requires_rollback_on_partial_failure": bool(planned),
            "items": planned}
    return plan, staged, paths, {"lote_id": lote["lote_id"], "lote_sha256": checksum(raw_lote),
                                 "decisions_sha256": checksum(raw_decisions), "results": settled}


def simulate(root: Path, raw_lote: bytes, lot_relative: str,
             raw_decisions: bytes) -> dict:
    plan, staged, paths, settlement = compile_transaction(root, raw_lote, lot_relative, raw_decisions)
    if not plan["items"]:
        return {"state": "SIMULATED_NO_WRITES", "count": len(settlement["results"]), "indexes_verified": True}
    changes = dict(staged)
    def read(p: str) -> bytes | None:
        if p in changes:
            return changes[p]
        file = root / p
        return file.read_bytes() if file.is_file() else None
    def write(p: str, b: bytes) -> bool:
        # Writes affect the in-memory overlay ONLY, never root.
        changes[p] = b
        return True
    report, errors = execute_canonical_transaction(
        plan, canonical_record_paths_before=paths, read_bytes=read,
        write_record_bytes=write, write_index_bytes=write)
    if errors or report["state"] != "CANONICAL_STATE_VERIFIED":
        raise PreflightError("transaction_simulation_failed")
    # Deliberate proof that local canonical bytes were not mutated.
    if checksum((root / INDEX_PATH).read_bytes()) != plan["master_index_sha256"]:
        raise PreflightError("unexpected_canonical_change")
    return {"state": "SIMULATED_VERIFIED", "count": len(settlement["results"]),
            "indexes_verified": report["final_verification"],
            "writes_simulated": report["record_report"]["writes_completed"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="E3: offline canonical simulation, no writes")
    parser.add_argument("--root-dir", type=Path, required=True)
    parser.add_argument("--date-brt", required=True)
    parser.add_argument("--sequence", type=int, default=1)
    args = parser.parse_args()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date_brt) or not 1 <= args.sequence <= 99:
        print("e3_error code=selection_invalid canonical_write=0")
        return 2
    filename = "lote.json" if args.sequence == 1 else "lote-" + f"{args.sequence:02d}" + ".json"
    decision_name = "decisoes.json" if args.sequence == 1 else "decisoes-" + f"{args.sequence:02d}" + ".json"
    relative = args.date_brt + "/" + filename
    try:
        lot = (args.root_dir / LOTES / relative).read_bytes()
        decisions = (args.root_dir / LOTES / args.date_brt / decision_name).read_bytes()
        if len(lot) > MAX_LOTE_BYTES or len(decisions) > MAX_DECISIONS_BYTES:
            raise PreflightError("private_input_too_large")
        result = simulate(args.root_dir, lot, relative, decisions)
    except (OSError, ValueError, PreflightError):
        print("e3_error code=preflight_failed canonical_write=0")
        return 2
    print("e3_ok status=" + result["state"] + " canonical_write=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
