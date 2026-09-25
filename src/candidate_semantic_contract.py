#!/usr/bin/env python3
"""Validate private semantic decisions for Technology Library candidates.

This module defines the boundary between mechanical candidate preflight and the
semantic validator. It does not call any model, does not research the web, and
never writes to 00_LIBRARY.

A semantic decision is accepted only when it is bound to the exact candidate
content hash that was indexed. This makes old decisions harmless if a candidate
changes before publication.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SEMANTIC_CONTRACT_VERSION = "0.2.0"
ID_RE = re.compile(r"^[0-9a-f]{20}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

ALLOWED_DECISIONS = {
    "VALIDATED_NEW",
    "VALIDATED_UPDATE",
    "DUPLICATE",
    "SOURCE_ONLY",
    "SPLIT_REQUIRED",
    "REJECTED",
    "SUSPECTED_ACCIDENTAL",
    "NEEDS_REVIEW",
}
ALLOWED_EVIDENCE = {"HIGH", "MEDIUM", "LOW", "CONFLICT"}


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def indexed_candidates(payload: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("candidate_index_invalid")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("candidate_index_invalid")

    output: dict[str, str] = {}
    for raw in entries:
        if not isinstance(raw, dict) or raw.get("valid") is not True:
            continue
        candidate_id = raw.get("candidate_id")
        sha256 = raw.get("sha256")
        if (
            isinstance(candidate_id, str)
            and ID_RE.fullmatch(candidate_id)
            and isinstance(sha256, str)
            and SHA_RE.fullmatch(sha256)
        ):
            output[candidate_id] = sha256
    return output


def validate_semantic_payload(
    payload: dict[str, Any] | None,
    candidate_index: dict[str, Any] | None,
) -> tuple[list[dict[str, str]], list[str]]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return [], ["semantic_payload_missing"]
    if set(payload) != {"schema_version", "semantic_contract_version", "items"}:
        errors.append("semantic_top_level_fields")
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("semantic_schema")
    if payload.get("semantic_contract_version") != SEMANTIC_CONTRACT_VERSION:
        errors.append("semantic_contract_version")

    try:
        current = indexed_candidates(candidate_index)
    except ValueError as exc:
        errors.append(str(exc))
        current = {}

    items = payload.get("items")
    if not isinstance(items, list) or len(items) > 500:
        errors.append("semantic_items")
        return [], sorted(set(errors))

    accepted: list[dict[str, str]] = []
    seen: set[str] = set()
    required = {"candidate_id", "candidate_sha256", "decision", "evidence_level"}

    for raw in items:
        if not isinstance(raw, dict) or set(raw) != required:
            errors.append("semantic_item_fields")
            continue
        candidate_id = raw.get("candidate_id")
        sha256 = raw.get("candidate_sha256")
        decision = raw.get("decision")
        evidence = raw.get("evidence_level")

        if not isinstance(candidate_id, str) or not ID_RE.fullmatch(candidate_id):
            errors.append("semantic_candidate_id")
            continue
        if candidate_id in seen:
            errors.append("semantic_duplicate_candidate")
            continue
        seen.add(candidate_id)

        if not isinstance(sha256, str) or not SHA_RE.fullmatch(sha256):
            errors.append("semantic_candidate_sha256")
            continue
        if decision not in ALLOWED_DECISIONS:
            errors.append("semantic_decision")
            continue
        if evidence not in ALLOWED_EVIDENCE:
            errors.append("semantic_evidence_level")
            continue

        current_sha = current.get(candidate_id)
        if current_sha is None:
            errors.append("semantic_candidate_not_current")
            continue
        if current_sha != sha256:
            errors.append("semantic_stale_candidate_revision")
            continue

        # Automatic canonical promotion must never be based on low/conflicting evidence.
        if decision in {"VALIDATED_NEW", "VALIDATED_UPDATE"} and evidence not in {"HIGH", "MEDIUM"}:
            errors.append("semantic_promotion_evidence_too_weak")
            continue

        accepted.append(
            {
                "candidate_id": candidate_id,
                "candidate_sha256": sha256,
                "decision": str(decision),
                "evidence_level": str(evidence),
            }
        )

    return accepted, sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate candidate semantic-decision contract.")
    parser.add_argument("--candidate-index", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    args = parser.parse_args()

    index_payload = read_json(args.candidate_index)
    decision_payload = read_json(args.decisions)
    accepted, errors = validate_semantic_payload(decision_payload, index_payload)

    if errors:
        print(
            "candidate_semantic_contract_error "
            f"accepted={len(accepted)} errors={','.join(errors)} canonical_write=0"
        )
        return 2

    counts: dict[str, int] = {}
    for item in accepted:
        counts[item["decision"]] = counts.get(item["decision"], 0) + 1
    print(
        "candidate_semantic_contract_ok "
        f"accepted={len(accepted)} outcomes={len(counts)} canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
