#!/usr/bin/env python3
"""Pure immutable E1/E3 lot settlement verifier.

A terminal receipt is an immutable sidecar, never a rewrite of lote.json.
This module has no I/O, network or write capabilities.
"""
from __future__ import annotations
import hashlib
import json
import re
from typing import Any

HEX20 = re.compile(r"^[0-9a-f]{20}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
LOT_PATH = re.compile(r"^(\d{4}-\d{2}-\d{2})/(lote(?:-\d{2})?\.json)$")
ACTIONS = {"CREATE", "UPDATE", "UNCHANGED", "REJECT", "HOLD"}


class LotStateError(ValueError):
    pass


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def parse(raw: bytes, *, limit: int) -> Any:
    if not raw or len(raw) > limit:
        raise LotStateError("sidecar_size_invalid")
    def unique(pairs):
        data = {}
        for k, v in pairs:
            if k in data:
                raise LotStateError("duplicate_json_key")
            data[k] = v
        return data
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise LotStateError("sidecar_json_invalid") from exc


def related_paths(relative_lot: str) -> tuple[str, str]:
    match = LOT_PATH.fullmatch(relative_lot)
    if not match:
        raise LotStateError("lot_path_invalid")
    day, name = match.groups()
    suffix = name[len("lote"):]
    return day + "/decisoes" + suffix, day + "/recibo" + suffix


def verify_settlement(
    raw_lot: bytes, relative_lot: str,
    raw_decisions: bytes, raw_receipt: bytes
) -> dict:
    decisions_path, receipt_path = related_paths(relative_lot)
    lote = parse(raw_lot, limit=200 * 1024)
    decisions = parse(raw_decisions, limit=400 * 1024)
    receipt = parse(raw_receipt, limit=120 * 1024)
    if not all(isinstance(v, dict) for v in (lote, decisions, receipt)):
        raise LotStateError("document_not_object")
    rows = lote.get("itens")
    choices = decisions.get("decisoes")
    results = receipt.get("resultados")
    if not all(isinstance(v, list) for v in (rows, choices, results)):
        raise LotStateError("rows_invalid")
    if not 1 <= len(rows) <= 20 or len(choices) != len(rows) or len(results) != len(rows):
        raise LotStateError("rows_count_mismatch")
    if (lote.get("schema_version") != 1 or lote.get("status") != "PRONTO"
            or not isinstance(lote.get("lote_id"), str)
            or decisions.get("schema_version") != 1
            or decisions.get("lote_id") != lote["lote_id"]
            or decisions.get("lote_sha256") != sha(raw_lot)
            or receipt.get("schema_version") != 1
            or receipt.get("lote_id") != lote["lote_id"]
            or receipt.get("lote_sha256") != sha(raw_lot)
            or receipt.get("decisoes_sha256") != sha(raw_decisions)
            or receipt.get("estado") != "APLICADO"
            or receipt.get("status_storage") != "IMMUTABLE_SIDECAR"
            or receipt.get("canonical_write_performed_by_receipt_planner") is not False):
        raise LotStateError("identity_binding_invalid")
    item_ids: list[str] = []
    for item in rows:
        if not isinstance(item, dict) or not isinstance(item.get("item_id"), str) or not HEX20.fullmatch(item["item_id"]):
            raise LotStateError("lot_item_invalid")
        item_ids.append(item["item_id"])
    if len(set(item_ids)) != len(item_ids):
        raise LotStateError("lot_duplicate_items")
    expected_ids = set(item_ids)
    actions: dict[str, str] = {}
    for choice in choices:
        if not isinstance(choice, dict):
            raise LotStateError("decision_item_invalid")
        key, act = choice.get("item_id"), choice.get("acao")
        if not isinstance(key, str) or key not in expected_ids or key in actions or act not in ACTIONS:
            raise LotStateError("decision_item_unbound")
        actions[key] = act
    seen: set[str] = set()
    changed = 0
    for result in results:
        if not isinstance(result, dict):
            raise LotStateError("result_item_invalid")
        key, act = result.get("item_id"), result.get("acao")
        if not isinstance(key, str) or key not in actions or key in seen or actions[key] != act:
            raise LotStateError("result_item_unbound")
        seen.add(key)
        before, after, record = (result.get(k) for k in ("before_sha256", "after_sha256", "record_id"))
        if act in {"CREATE", "UPDATE"}:
            changed += 1
            if (not isinstance(record, str) or not HEX20.fullmatch(record)
                    or not isinstance(after, str) or not HEX64.fullmatch(after)
                    or (act == "CREATE" and before is not None)
                    or (act == "UPDATE" and (not isinstance(before, str) or not HEX64.fullmatch(before)))):
                raise LotStateError("result_write_hash_invalid")
        elif any(v is not None for v in (before, after, record)):
            raise LotStateError("result_nonwrite_hash_invalid")
    if changed:
        if (receipt.get("index_verification") != "VERIFIED"
                or not all(isinstance(receipt.get(k), str) and HEX64.fullmatch(receipt[k])
                           for k in ("master_before_sha256", "master_after_sha256"))
                or not isinstance(receipt.get("transaction_id"), str)
                or not HEX20.fullmatch(receipt["transaction_id"])
                or not isinstance(receipt.get("commit_receipt_sha256"), str)
                or not HEX64.fullmatch(receipt["commit_receipt_sha256"])):
            raise LotStateError("committed_proof_missing")
    elif (receipt.get("index_verification") != "UNCHANGED"
          or receipt.get("transaction_id") is not None
          or receipt.get("commit_receipt_sha256") is not None):
        raise LotStateError("nonwrite_proof_invalid")
    return {"lote_id": lote["lote_id"], "settled": True, "writes": changed,
            "decisions_path": decisions_path, "receipt_path": receipt_path}
