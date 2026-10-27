#!/usr/bin/env python3
"""E3: pure terminal-receipt planner for immutable E1 lots.

No storage writes, no model calls, no canonical mutations. Real publication
must already have a verified committed journal and a sealed transaction receipt.
"""
from __future__ import annotations

import hashlib
from typing import Any, Callable

from e1_lote_prepare import encode
from e2_gemini_decide import (
    DecisionError, MODEL, strict_json, validate_lote, validate_model_output,
)

ReadCanonical = Callable[[str], bytes | None]


class SettlementError(Exception):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_terminal_receipt(
    raw_lote: bytes,
    lot_relative: str,
    raw_decisions: bytes,
    *,
    commit_journal: dict[str, Any] | None,
    verified_receipt_raw: bytes | None,
    read_canonical: ReadCanonical | None = None,
) -> dict:
    try:
        lote = validate_lote(raw_lote, lot_relative)
        response = strict_json(raw_decisions)
        if (not isinstance(response, dict)
                or response.get("schema_version") != 1
                or response.get("lote_id") != lote["lote_id"]
                or response.get("lote_sha256") != sha(raw_lote)
                or response.get("model") != MODEL
                or response.get("canonical_write_performed") is not False):
            raise SettlementError("decision_document_unbound")
        decisions = validate_model_output(lote, {"decisoes": response.get("decisoes")})
    except DecisionError:
        raise SettlementError("decisions_invalid") from None

    written = [d for d in decisions if d["acao"] in {"CREATE", "UPDATE"}]
    master_before = None
    master_after = None
    index_status = "UNCHANGED"
    rows_by_id: dict[str, dict] = {}
    journal_entries: dict[str, dict] = {}
    terminal_transaction_id = None
    terminal_commit_receipt_sha = None

    if written:
        if not isinstance(commit_journal, dict) or not isinstance(verified_receipt_raw, bytes):
            raise SettlementError("commit_evidence_missing")
        try:
            receipt = strict_json(verified_receipt_raw)
        except DecisionError:
            raise SettlementError("committed_receipt_invalid") from None
        if not isinstance(receipt, dict):
            raise SettlementError("committed_receipt_invalid")
        tx = commit_journal.get("transaction_id")
        if (commit_journal.get("state") != "COMMITTED"
                or commit_journal.get("canonical_write_performed") is not True
                or commit_journal.get("snapshots_verified") is not True
                or commit_journal.get("live_preconditions_verified_under_ownership") is not True
                or commit_journal.get("coordinator_release_allowed") is not True
                or commit_journal.get("receipt_sha256") != sha(verified_receipt_raw)
                or commit_journal.get("readiness_sha256") != sha(raw_lote)
                or commit_journal.get("authorization_sha256") != sha(raw_decisions)
                or receipt.get("state") != "VERIFIED_COMMITTED_STATE"
                or receipt.get("verification_only") is not True
                or receipt.get("candidate_settlement_eligible") is not True
                or receipt.get("transaction_id") != tx):
            raise SettlementError("transaction_unverified")
        if read_canonical is None:
            raise SettlementError("canonical_read_required")
        entries = commit_journal.get("items")
        receipt_rows = receipt.get("items")
        if not isinstance(entries, list) or not isinstance(receipt_rows, list):
            raise SettlementError("transaction_inventory_invalid")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("target_path"), str):
                raise SettlementError("transaction_inventory_invalid")
            target = entry["target_path"]
            if target in journal_entries:
                raise SettlementError("transaction_duplicate_target")
            journal_entries[target] = entry
        for row in receipt_rows:
            if not isinstance(row, dict) or not isinstance(row.get("candidate_id"), str):
                raise SettlementError("receipt_inventory_invalid")
            key = row["candidate_id"]
            if key in rows_by_id:
                raise SettlementError("receipt_duplicate_item")
            rows_by_id[key] = row
        if len(rows_by_id) != len(written) or len(journal_entries) != len(written):
            raise SettlementError("receipt_item_count_mismatch")
        master_before = commit_journal.get("master_index_before_sha256")
        master_after = receipt.get("master_index_after_sha256")
        if (not isinstance(master_before, str) or len(master_before) != 64
                or not isinstance(master_after, str) or len(master_after) != 64):
            raise SettlementError("master_hash_invalid")
        live_master = read_canonical("00_LIBRARY/MASTER_INDEX.md")
        if live_master is None or sha(live_master) != master_after:
            raise SettlementError("master_commit_changed")
        index_status = "VERIFIED"
        terminal_transaction_id = tx
        terminal_commit_receipt_sha = sha(verified_receipt_raw)
    else:
        if commit_journal is not None or verified_receipt_raw is not None:
            raise SettlementError("unexpected_commit_for_nonwrite")

    outcomes: list[dict] = []
    seen_targets = set()
    for decision in decisions:
        item_id, action = decision["item_id"], decision["acao"]
        outcome: dict[str, Any] = {
            "item_id": item_id, "acao": action,
            "record_id": None, "before_sha256": None, "after_sha256": None,
            "motivo": decision["motivo"],
        }
        if action in {"CREATE", "UPDATE"}:
            row = rows_by_id.pop(item_id, None)
            if row is None or row.get("action") != action:
                raise SettlementError("receipt_action_mismatch")
            target = row.get("target_path")
            entry = journal_entries.get(target)
            if (not isinstance(target, str) or target in seen_targets
                    or entry is None or entry.get("action") != action
                    or entry.get("record_id") != row.get("record_id")
                    or row.get("record_id") != (
                        decision["target_record_id"] if action == "UPDATE"
                        else hashlib.sha256(("e2-record-v1|" + item_id).encode()).hexdigest()[:20]
                    )):
                raise SettlementError("committed_item_identity_mismatch")
            seen_targets.add(target)
            live = read_canonical(target)
            after = row.get("content_sha256")
            before = entry.get("before_sha256")
            if (not isinstance(after, str) or len(after) != 64
                    or live is None or sha(live) != after
                    or (action == "CREATE" and before is not None)
                    or (action == "UPDATE" and (not isinstance(before, str) or len(before) != 64))):
                raise SettlementError("committed_content_changed")
            outcome.update(record_id=row["record_id"], before_sha256=before,
                           after_sha256=after)
        outcomes.append(outcome)

    result = {"schema_version": 1, "lote_id": lote["lote_id"],
              "lote_sha256": sha(raw_lote), "decisoes_sha256": sha(raw_decisions),
              "estado": "APLICADO", "status_storage": "IMMUTABLE_SIDECAR",
              "resultados": outcomes, "index_verification": index_status,
              "master_before_sha256": master_before,
              "master_after_sha256": master_after,
              "transaction_id": terminal_transaction_id,
              "commit_receipt_sha256": terminal_commit_receipt_sha,
              "canonical_write_performed_by_receipt_planner": False}
    if len(encode(result)) > 120 * 1024:
        raise SettlementError("terminal_receipt_too_large")
    return result
