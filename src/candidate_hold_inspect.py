#!/usr/bin/env python3
"""Diagnose READY_FOR_CURATION candidates omitted from the sealed current batch.

The finalizer intentionally fails closed, but its compact terminal summary cannot
explain every held candidate. This read-only helper classifies the persisted state
that is safe to inspect deterministically: claim-review state, curation binding,
editorial usefulness and whether the candidate appears in the authoritative current
NEW or UPDATE selection.

It does not rerun Ollama, does not modify private state and never writes 00_LIBRARY.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_batch_inspect import (
    DEFAULT_BATCH,
    DEFAULT_MASTER,
    DEFAULT_PUBLISH,
    DEFAULT_ROOT,
    DEFAULT_UPDATE,
    DEFAULT_VALIDATION,
    inspect_batch,
    read_json_bytes,
    read_local,
    read_remote,
)
from candidate_curation_prepare import list_ready
from candidate_editorial_gate import apply_editorial_gate
from candidate_publish_index import parse_candidate_manifest
from candidate_transaction_prepare import parse_update_manifest

SCHEMA_VERSION = 1
HOLD_INSPECT_VERSION = "0.1.0"
DEFAULT_READY = "READY_FOR_CURATION"
DEFAULT_CLAIMS = "CLAIM_REVIEWS"
DEFAULT_CURATIONS = "CURATION_DRAFTS"


def classify_candidate(
    *,
    candidate_id: str,
    claim_review: dict[str, Any] | None,
    curation: dict[str, Any] | None,
    selected_new_keys: set[tuple[str, str]],
    selected_update_ids: set[str],
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "candidate_id": candidate_id,
        "state": "HELD_OR_UNSELECTED",
        "reason": None,
        "decision": curation.get("decision") if isinstance(curation, dict) else None,
        "title": None,
        "claim_state": claim_review.get("state") if isinstance(claim_review, dict) else None,
        "claim_counts": claim_review.get("counts") if isinstance(claim_review, dict) else None,
        "editorial_state": None,
        "editorial_reasons": [],
    }
    proposed = curation.get("proposed_record") if isinstance(curation, dict) else None
    if isinstance(proposed, dict):
        base["title"] = proposed.get("title")

    if not isinstance(claim_review, dict):
        base["reason"] = "CLAIM_REVIEW_MISSING"
        return base
    if claim_review.get("state") != "READY":
        base["reason"] = f"CLAIM_REVIEW_{str(claim_review.get('state') or 'INVALID')}"
        return base
    if not isinstance(curation, dict):
        base["reason"] = "CURATION_DRAFT_MISSING"
        return base
    if curation.get("candidate_id") != candidate_id:
        base["reason"] = "CURATION_CANDIDATE_BINDING_MISMATCH"
        return base

    decision = curation.get("decision")
    package_id = curation.get("package_id")
    revision_key = curation.get("revision_key")
    if decision == "VALIDATED_UPDATE" and candidate_id in selected_update_ids:
        base["state"] = "SELECTED"
        base["reason"] = "CURRENT_BATCH_UPDATE"
        return base
    if (
        decision in {"VALIDATED_NEW", "SOURCE_ONLY"}
        and isinstance(package_id, str)
        and isinstance(revision_key, str)
        and (package_id, revision_key) in selected_new_keys
    ):
        base["state"] = "SELECTED"
        base["reason"] = "CURRENT_BATCH_NEW"
        return base

    if not isinstance(proposed, dict):
        base["reason"] = "PROPOSED_RECORD_MISSING"
        return base
    editorial, editorial_errors = apply_editorial_gate(
        proposed,
        claim_review,
        require_functional_evidence=decision != "VALIDATED_UPDATE",
    )
    if editorial_errors or editorial is None:
        base["reason"] = "EDITORIAL_GATE_ERROR"
        base["editorial_reasons"] = editorial_errors
        return base
    base["editorial_state"] = editorial.get("state")
    reasons = editorial.get("reasons") if isinstance(editorial.get("reasons"), list) else []
    base["editorial_reasons"] = reasons
    if editorial.get("state") != "READY":
        base["reason"] = "EDITORIAL_HOLD"
        return base

    if decision == "VALIDATED_UPDATE":
        base["reason"] = "DOWNSTREAM_UPDATE_PREPARATION_MISSING_OR_FAILED"
    elif decision in {"VALIDATED_NEW", "SOURCE_ONLY"}:
        base["reason"] = "DOWNSTREAM_NEW_PREPARATION_MISSING_OR_FAILED"
    else:
        base["reason"] = "CURATION_DECISION_NOT_PUBLISHABLE"
    return base


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose held/unselected candidates without rerunning semantic work.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root-dir", type=Path)
    mode.add_argument("--remote")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    root_base = args.root.strip("/")
    publish_rel = f"{root_base}/{DEFAULT_PUBLISH}"
    validation_rel = f"{root_base}/{DEFAULT_VALIDATION}"
    update_rel = f"{root_base}/{DEFAULT_UPDATE}"
    batch_rel = f"{root_base}/{DEFAULT_BATCH}"

    if args.root_dir is not None:
        root = args.root_dir.resolve()
        reader = lambda relative: read_local(root, relative)
        ready_dir = root / root_base / DEFAULT_READY
        names = sorted(path.name for path in ready_dir.glob("candidate-*.md") if path.is_file()) if ready_dir.exists() else []
    else:
        if not shutil.which("rclone"):
            print("candidate_hold_inspect_error code=rclone_missing canonical_write=0")
            return 2
        remote = str(args.remote)
        reader = lambda relative: read_remote(remote, relative)
        remote_names = list_ready(remote, root_base, DEFAULT_READY)
        if remote_names is None:
            print("candidate_hold_inspect_error code=ready_queue_unavailable canonical_write=0")
            return 2
        names = remote_names

    batch_report, batch_errors = inspect_batch(
        master_raw=reader(DEFAULT_MASTER),
        publish_raw=reader(publish_rel),
        validation_raw=reader(validation_rel),
        update_raw=reader(update_rel),
        batch_raw=reader(batch_rel),
        read_content=reader,
    )
    if batch_errors or batch_report is None:
        print(
            "candidate_hold_inspect_error "
            f"codes={','.join(batch_errors or ['sealed_batch_invalid'])} canonical_write=0"
        )
        return 2

    publish_payload = read_json_bytes(reader(publish_rel))
    update_payload = read_json_bytes(reader(update_rel))
    if publish_payload is None or update_payload is None:
        print("candidate_hold_inspect_error code=current_selection_missing canonical_write=0")
        return 2
    try:
        new_items = parse_candidate_manifest(publish_payload)
    except ValueError as exc:
        print(f"candidate_hold_inspect_error code=publish_manifest_{exc} canonical_write=0")
        return 2
    updates, update_errors = parse_update_manifest(update_payload)
    if update_errors:
        print(f"candidate_hold_inspect_error codes={','.join(update_errors)} canonical_write=0")
        return 2

    selected_new_keys = {(item["package_id"], item["revision_key"]) for item in new_items}
    selected_update_ids = {
        str(item.get("candidate_id")) for item in updates if isinstance(item.get("candidate_id"), str)
    }

    rows: list[dict[str, Any]] = []
    for name in names:
        candidate_rel = str(PurePosixPath(root_base) / DEFAULT_READY / name)
        raw = reader(candidate_rel)
        if raw is None:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        candidate_id = digest[:20]
        claim_rel = str(PurePosixPath(root_base) / DEFAULT_CLAIMS / f"{candidate_id}.json")
        curation_rel = str(PurePosixPath(root_base) / DEFAULT_CURATIONS / candidate_id / "draft.json")
        row = classify_candidate(
            candidate_id=candidate_id,
            claim_review=read_json_bytes(reader(claim_rel)),
            curation=read_json_bytes(reader(curation_rel)),
            selected_new_keys=selected_new_keys,
            selected_update_ids=selected_update_ids,
        )
        row["candidate_file"] = name
        rows.append(row)

    rows.sort(key=lambda row: (0 if row["state"] != "SELECTED" else 1, str(row.get("title") or ""), row["candidate_id"]))
    held = [row for row in rows if row["state"] != "SELECTED"]
    report = {
        "schema_version": SCHEMA_VERSION,
        "hold_inspect_version": HOLD_INSPECT_VERSION,
        "batch_id": batch_report.get("batch_id"),
        "canonical_write_performed": False,
        "counts": {"ready_candidates": len(rows), "selected": len(rows) - len(held), "held_or_unselected": len(held)},
        "items": rows,
    }
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "candidate_hold_inspect_ok "
        f"batch_id={report.get('batch_id')} ready={len(rows)} selected={len(rows) - len(held)} "
        f"held_or_unselected={len(held)} canonical_write=0"
    )
    for row in held:
        reasons = ",".join(row.get("editorial_reasons") or []) or "none"
        print(
            "candidate_hold_item "
            f"candidate_id={row['candidate_id']} reason={row['reason']} "
            f"editorial_reasons={reasons} title={json.dumps(row.get('title'), ensure_ascii=False)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
