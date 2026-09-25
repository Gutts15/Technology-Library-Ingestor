#!/usr/bin/env python3
"""Stage rendered VALIDATED_UPDATE artifacts in isolated private UPDATE_READY.

This stage bridges SHA-bound update planning and transaction planning without
weakening the canonical publication boundary. Each rendered update is bound to
the exact canonical base bytes. A separate ``latest.json`` manifest identifies
only the artifacts produced by the current finalization batch so stale private
files from older runs cannot leak into a later transaction.

It has no canonical apply mode and never writes to 00_LIBRARY.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from candidate_update_render import MERGE_RENDER_VERSION, read_json, render_update

SCHEMA_VERSION = 1
UPDATE_READY_VERSION = "0.2.0"
DEFAULT_ROOT = "99_INBOX/CANDIDATES"
DEFAULT_READY = "UPDATE_READY"
MAX_ITEMS = 50


def build_ready_artifact(
    plan: dict[str, Any] | None,
    base_content: bytes,
    *,
    root: str = DEFAULT_ROOT,
    ready_dir: str = DEFAULT_READY,
) -> tuple[bytes | None, dict[str, Any] | None, list[str]]:
    """Render and package one update for the isolated private update lane."""

    content, metadata, errors = render_update(plan, base_content)
    if errors or content is None or metadata is None:
        return None, None, errors or ["update_render_failed"]

    record_id = metadata.get("record_id")
    if not isinstance(record_id, str) or not record_id:
        return None, None, ["rendered_record_id_missing"]
    target_path = metadata.get("target_path")
    if not isinstance(target_path, str) or not target_path.startswith("00_LIBRARY/"):
        return None, None, ["rendered_target_path_invalid"]
    candidate_id = plan.get("candidate_id") if isinstance(plan, dict) else None
    if not isinstance(candidate_id, str) or len(candidate_id) != 20:
        return None, None, ["candidate_id_missing"]

    content_path = f"{root.strip('/')}/{ready_dir.strip('/')}/records/{record_id}.md"
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "update_ready_version": UPDATE_READY_VERSION,
        "merge_render_version": MERGE_RENDER_VERSION,
        "candidate_id": candidate_id,
        "record_id": record_id,
        "target_path": target_path,
        "base_sha256": metadata["base_sha256"],
        "merged_sha256": metadata["merged_sha256"],
        "content_path": content_path,
        "proposed_status": metadata.get("proposed_status"),
        "claims_input": metadata.get("claims_input"),
        "claims_added": metadata.get("claims_added"),
        "claims_deduped": metadata.get("claims_deduped"),
        "requires_base_sha_match_before_write": True,
        "canonical_write_performed": False,
    }
    return content, artifact, []


def build_ready_manifest(
    artifacts: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Build the authoritative current-batch UPDATE_READY selection."""

    if len(artifacts) > MAX_ITEMS:
        return None, ["too_many_update_ready_items"]
    errors: list[str] = []
    seen_candidates: set[str] = set()
    seen_records: set[str] = set()
    seen_targets: set[str] = set()
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(artifacts):
        if not isinstance(raw, dict):
            errors.append(f"item_{index}_invalid")
            continue
        if raw.get("schema_version") != SCHEMA_VERSION:
            errors.append(f"item_{index}_schema")
        if raw.get("update_ready_version") != UPDATE_READY_VERSION:
            errors.append(f"item_{index}_version")
        if raw.get("canonical_write_performed") is not False:
            errors.append(f"item_{index}_write_flag")
        candidate_id = raw.get("candidate_id")
        record_id = raw.get("record_id")
        target = raw.get("target_path")
        content_path = raw.get("content_path")
        if not isinstance(candidate_id, str) or len(candidate_id) != 20:
            errors.append(f"item_{index}_candidate_id")
        if not isinstance(record_id, str) or len(record_id) != 20:
            errors.append(f"item_{index}_record_id")
        if not isinstance(target, str) or not target.startswith("00_LIBRARY/"):
            errors.append(f"item_{index}_target")
        if not isinstance(content_path, str) or not content_path.startswith(
            f"{DEFAULT_ROOT}/{DEFAULT_READY}/records/"
        ):
            errors.append(f"item_{index}_content_path")
        if isinstance(candidate_id, str):
            if candidate_id in seen_candidates:
                errors.append(f"item_{index}_duplicate_candidate")
            seen_candidates.add(candidate_id)
        if isinstance(record_id, str):
            if record_id in seen_records:
                errors.append(f"item_{index}_duplicate_record")
            seen_records.add(record_id)
        if isinstance(target, str):
            if target in seen_targets:
                errors.append(f"item_{index}_duplicate_target")
            seen_targets.add(target)
        items.append(dict(raw))
    if errors:
        return None, sorted(set(errors))
    return {
        "schema_version": SCHEMA_VERSION,
        "update_ready_version": UPDATE_READY_VERSION,
        "canonical_write_performed": False,
        "items": sorted(items, key=lambda item: (str(item["target_path"]), str(item["candidate_id"]))),
    }, []


def write_local_ready(
    root_dir: Path,
    content: bytes,
    artifact: dict[str, Any],
) -> tuple[Path, Path]:
    content_rel = artifact["content_path"]
    candidate_id = artifact["candidate_id"]
    metadata_rel = f"{DEFAULT_ROOT}/{DEFAULT_READY}/{candidate_id}.json"
    content_path = root_dir / content_rel
    metadata_path = root_dir / metadata_rel
    content_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    content_path.write_bytes(content)
    metadata_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return content_path, metadata_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render and stage one SHA-bound candidate update in private UPDATE_READY."
    )
    parser.add_argument("--update-plan", type=Path, required=True)
    parser.add_argument("--base-record", type=Path, required=True)
    parser.add_argument("--root-dir", type=Path, required=True)
    args = parser.parse_args()

    plan = read_json(args.update_plan)
    try:
        base_content = args.base_record.read_bytes()
    except OSError:
        print("candidate_update_ready_error code=base_record_read_failed canonical_write=0")
        return 2

    content, artifact, errors = build_ready_artifact(plan, base_content)
    if errors or content is None or artifact is None:
        print(
            "candidate_update_ready_error "
            f"codes={','.join(errors or ['prepare_failed'])} canonical_write=0"
        )
        return 2

    try:
        _, metadata_path = write_local_ready(args.root_dir.resolve(), content, artifact)
    except OSError:
        print("candidate_update_ready_error code=private_stage_write_failed canonical_write=0")
        return 2

    print(
        "candidate_update_ready_ok "
        f"record_id={artifact['record_id']} metadata={metadata_path.name} "
        f"update_ready_version={UPDATE_READY_VERSION} canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
