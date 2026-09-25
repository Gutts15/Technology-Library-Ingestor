#!/usr/bin/env python3
"""Finalize semantically approved chat candidates with local Ollama, without publishing.

This local-only stage is intended to run after candidate_semantic_batch.py and
candidate_resolution.py have moved validated candidates into READY_FOR_CURATION.
It refreshes bounded explicit-source evidence, performs claim-level review,
applies canonical editorial filtering through the downstream renderers, prepares
private curation/canonical artifacts, creates an isolated candidate publish outbox
for new/source-only records, and creates SHA-bound update plans plus rendered
private UPDATE_READY artifacts for VALIDATED_UPDATE.

Each run rewrites the authoritative current-batch PUBLISH_READY/latest.json and
UPDATE_READY/latest.json selections, including empty selections. After the
new-lane dry-run plan is written, the exact control-file bytes are sealed by one
FINALIZE_BATCH/current.json document written last. Transaction preparation can
therefore reject torn/mixed control state from concurrent finalizer runs.

It never writes to 00_LIBRARY and has no canonical --apply option.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_canonical_draft import build_markdown
from candidate_claim_review import build_prompt as build_claim_prompt
from candidate_claim_review import build_review as build_claim_review
from candidate_claim_review import call_ollama as call_claim_ollama
from candidate_curation_prepare import build_draft, list_ready, remote_json, remote_text, upload_pair
from candidate_finalize_batch import (
    build_finalize_batch,
    write_remote_atomic as write_finalize_batch_atomic,
)
from candidate_publish_index import write_remote_atomic as write_publish_latest_atomic
from candidate_publish_prepare import build_content, build_manifest, write_outbox
from candidate_publish_validate import assess_publication
from candidate_semantic_local import endpoint_is_loopback
from candidate_source_probe import build_probe as build_source_probe
from candidate_update_plan import build_update_plan
from candidate_update_ready import build_ready_artifact, build_ready_manifest
from library_publish import derive_target

SCHEMA_VERSION = 1
FINALIZE_VERSION = "0.6.0"
DEFAULT_ROOT = "99_INBOX/CANDIDATES"
DEFAULT_READY = "READY_FOR_CURATION"
DEFAULT_SEMANTIC_REVIEWS = "SEMANTIC_REVIEWS"
DEFAULT_FINAL_SOURCE_PROBES = "FINAL_SOURCE_PROBES"
DEFAULT_CLAIM_REVIEWS = "CLAIM_REVIEWS"
DEFAULT_CURATON_DRAFTS = "CURATION_DRAFTS"
DEFAULT_CANONICAL_DRAFTS = "CANONICAL_DRAFTS"
DEFAULT_UPDATE_PLANS = "UPDATE_PLANS"
DEFAULT_UPDATE_READY = "UPDATE_READY"
DEFAULT_PUBLISH_OUTBOX = "PUBLISH_READY"
DEFAULT_FINALIZE_BATCH = "FINALIZE_BATCH/current.json"
DEFAULT_MASTER = "00_LIBRARY/MASTER_INDEX.md"
MAX_ITEMS = 20


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["rclone", *arguments], check=False, capture_output=True)


def remote_bytes(remote: str, relative: str) -> bytes | None:
    result = run_rclone(["cat", join_remote(remote, relative), "--log-level", "ERROR"])
    return result.stdout if result.returncode == 0 else None


def upload_bytes(remote: str, relative: str, content: bytes) -> bool:
    parent = str(PurePosixPath(relative).parent)
    if parent not in {"", "."}:
        mkdir = run_rclone(["mkdir", join_remote(remote, parent), "--log-level", "ERROR"])
        if mkdir.returncode != 0:
            return False
    with tempfile.NamedTemporaryFile(prefix="tl-candidate-finalize-", delete=False) as handle:
        handle.write(content)
        local_name = handle.name
    try:
        result = run_rclone([
            "copyto",
            local_name,
            join_remote(remote, relative),
            "--log-level",
            "ERROR",
            "--stats",
            "0",
        ])
        return result.returncode == 0
    finally:
        try:
            Path(local_name).unlink()
        except OSError:
            pass


def upload_json(remote: str, relative: str, payload: dict[str, Any]) -> bool:
    return upload_bytes(
        remote,
        relative,
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def semantic_probe(review: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(review, dict):
        return None
    probe = review.get("source_probe")
    if not isinstance(probe, dict) or probe.get("schema_version") != 1:
        return None
    return probe


def probe_success_count(probe: dict[str, Any] | None) -> int:
    if not isinstance(probe, dict) or probe.get("schema_version") != 1:
        return 0
    value = probe.get("successful_sources")
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    sources = probe.get("sources")
    if not isinstance(sources, list):
        return 0
    return sum(1 for item in sources if isinstance(item, dict) and item.get("status") == "OK")


def choose_final_probe(
    stored: dict[str, Any] | None,
    refreshed: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str]:
    """Prefer fresh explicit-source evidence; fail back only to prior valid evidence."""

    if probe_success_count(refreshed) > 0:
        return refreshed, "REFRESHED"
    if probe_success_count(stored) > 0:
        return stored, "STORED_FALLBACK"
    return None, "UNAVAILABLE"


def write_canonical_draft(
    remote: str,
    root: str,
    candidate_id: str,
    markdown: str,
    metadata: dict[str, Any],
) -> bool:
    base = str(PurePosixPath(root) / DEFAULT_CANONICAL_DRAFTS / candidate_id)
    return upload_bytes(remote, f"{base}/draft.md", markdown.encode("utf-8")) and upload_json(
        remote, f"{base}/metadata.json", metadata
    )


def write_update_plan(
    remote: str,
    root: str,
    candidate_id: str,
    plan: dict[str, Any],
) -> bool:
    relative = str(PurePosixPath(root) / DEFAULT_UPDATE_PLANS / f"{candidate_id}.json")
    return upload_json(remote, relative, plan)


def write_update_ready(
    remote: str,
    root: str,
    candidate_id: str,
    content: bytes,
    artifact: dict[str, Any],
) -> bool:
    content_path = artifact.get("content_path")
    expected_prefix = str(PurePosixPath(root) / DEFAULT_UPDATE_READY / "records") + "/"
    if not isinstance(content_path, str) or not content_path.startswith(expected_prefix):
        return False
    metadata_path = str(
        PurePosixPath(root) / DEFAULT_UPDATE_READY / f"{candidate_id}.json"
    )
    return upload_bytes(remote, content_path, content) and upload_json(
        remote, metadata_path, artifact
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh evidence, claim-review and prepare semantically approved candidates using loopback Ollama only."
    )
    parser.add_argument("--remote", default="tl:")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--model", required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--source-timeout", type=float, default=8.0)
    parser.add_argument("--max-items", type=int, default=MAX_ITEMS)
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("candidate_finalize_local_error code=rclone_missing canonical_write=0")
        return 2
    if not endpoint_is_loopback(args.endpoint):
        print("candidate_finalize_local_error code=non_loopback_model_endpoint canonical_write=0")
        return 2

    master_text = remote_text(args.remote, DEFAULT_MASTER)
    if master_text is None:
        print("candidate_finalize_local_error code=master_index_missing canonical_write=0")
        return 2
    names = list_ready(args.remote, args.root, DEFAULT_READY)
    if names is None:
        print("candidate_finalize_local_error code=ready_queue_unavailable canonical_write=0")
        return 2
    names = names[: max(1, min(int(args.max_items), MAX_ITEMS))]

    prepared_items: list[dict[str, Any]] = []
    prepared_contents: dict[str, bytes | None] = {}
    update_artifacts: list[dict[str, Any]] = []
    update_planned = 0
    updates_ready = 0
    held = 0
    source_refreshed = 0
    source_fallback = 0

    for name in names:
        candidate_rel = str(PurePosixPath(args.root) / DEFAULT_READY / name)
        candidate_text = remote_text(args.remote, candidate_rel)
        if candidate_text is None:
            held += 1
            continue
        candidate_sha = hashlib.sha256(candidate_text.encode("utf-8")).hexdigest()
        candidate_id = candidate_sha[:20]

        semantic_rel = str(
            PurePosixPath(args.root) / DEFAULT_SEMANTIC_REVIEWS / f"{candidate_id}.json"
        )
        semantic_review = remote_json(args.remote, semantic_rel)
        stored_probe = semantic_probe(semantic_review)
        if semantic_review is None or stored_probe is None:
            held += 1
            continue

        refreshed_probe = build_source_probe(
            candidate_text,
            timeout=max(1.0, min(float(args.source_timeout), 20.0)),
        )
        probe, probe_mode = choose_final_probe(stored_probe, refreshed_probe)
        if probe is None:
            held += 1
            continue
        if probe_mode == "REFRESHED":
            source_refreshed += 1
        else:
            source_fallback += 1

        probe_rel = str(
            PurePosixPath(args.root) / DEFAULT_FINAL_SOURCE_PROBES / f"{candidate_id}.json"
        )
        if not upload_json(args.remote, probe_rel, probe):
            print("candidate_finalize_local_error code=final_source_probe_write_failed canonical_write=0")
            return 2

        model_payload = call_claim_ollama(
            args.endpoint,
            args.model,
            build_claim_prompt(candidate_text, probe),
            max(10.0, min(float(args.timeout), 600.0)),
        )
        claim_review, claim_errors = build_claim_review(candidate_text, probe, model_payload)
        if claim_errors or claim_review is None:
            held += 1
            continue

        claim_rel = str(
            PurePosixPath(args.root) / DEFAULT_CLAIM_REVIEWS / f"{candidate_id}.json"
        )
        if not upload_json(args.remote, claim_rel, claim_review):
            print("candidate_finalize_local_error code=claim_review_write_failed canonical_write=0")
            return 2
        if claim_review.get("state") != "READY":
            held += 1
            continue

        package, material, curation_errors = build_draft(
            candidate_text, semantic_review, master_text
        )
        if curation_errors or package is None or material is None:
            held += 1
            continue
        if not upload_pair(
            args.remote,
            args.root,
            DEFAULT_CURATON_DRAFTS,
            candidate_id,
            package,
            material,
        ):
            print("candidate_finalize_local_error code=curation_draft_write_failed canonical_write=0")
            return 2

        if package.get("decision") == "VALIDATED_UPDATE":
            proposed = package.get("proposed_record")
            target = proposed.get("target_path") if isinstance(proposed, dict) else None
            if not isinstance(target, str) or not target.startswith("00_LIBRARY/"):
                held += 1
                continue
            base_content = remote_bytes(args.remote, target)
            if base_content is None:
                held += 1
                continue
            update_plan, update_errors = build_update_plan(
                package, claim_review, probe, base_content, target
            )
            if update_errors or update_plan is None:
                held += 1
                continue
            if not write_update_plan(
                args.remote, args.root, candidate_id, update_plan
            ):
                print("candidate_finalize_local_error code=update_plan_write_failed canonical_write=0")
                return 2
            update_planned += 1

            update_content, update_artifact, ready_errors = build_ready_artifact(
                update_plan,
                base_content,
                root=args.root,
                ready_dir=DEFAULT_UPDATE_READY,
            )
            if ready_errors or update_content is None or update_artifact is None:
                held += 1
                continue
            if not write_update_ready(
                args.remote,
                args.root,
                candidate_id,
                update_content,
                update_artifact,
            ):
                print("candidate_finalize_local_error code=update_ready_write_failed canonical_write=0")
                return 2
            update_artifacts.append(update_artifact)
            updates_ready += 1
            continue

        canonical_draft, canonical_meta, canonical_errors = build_markdown(
            package, claim_review, probe
        )
        if canonical_errors or canonical_draft is None or canonical_meta is None:
            held += 1
            continue
        if not write_canonical_draft(
            args.remote, args.root, candidate_id, canonical_draft, canonical_meta
        ):
            print("candidate_finalize_local_error code=canonical_draft_write_failed canonical_write=0")
            return 2

        content, item, publish_errors = build_content(package, claim_review, probe)
        if publish_errors or content is None or item is None:
            held += 1
            continue
        manifest, manifest_errors = build_manifest([item])
        if manifest_errors or manifest is None:
            held += 1
            continue
        if not write_outbox(args.remote, args.root, candidate_id, content, manifest):
            print("candidate_finalize_local_error code=isolated_outbox_write_failed canonical_write=0")
            return 2
        prepared_items.append(item)
        prepared_contents[item["content_path"]] = content

    # Aggregate only candidates successfully prepared in this run. Rewriting
    # even empty selections is intentional: stale per-candidate artifacts may
    # remain for audit, but they are no longer transaction inputs.
    latest, aggregate_errors = build_manifest(prepared_items)
    if aggregate_errors or latest is None:
        print("candidate_finalize_local_error code=aggregate_manifest_failed canonical_write=0")
        return 2
    latest_rel = str(PurePosixPath(args.root) / DEFAULT_PUBLISH_OUTBOX / "latest.json")
    if not write_publish_latest_atomic(args.remote, latest_rel, latest):
        print("candidate_finalize_local_error code=aggregate_manifest_write_failed canonical_write=0")
        return 2
    stored_latest = remote_json(args.remote, latest_rel)
    if stored_latest != latest:
        print("candidate_finalize_local_error code=aggregate_manifest_verify_failed canonical_write=0")
        return 2

    update_latest, update_latest_errors = build_ready_manifest(update_artifacts)
    if update_latest_errors or update_latest is None:
        print("candidate_finalize_local_error code=update_ready_manifest_failed canonical_write=0")
        return 2
    update_latest_rel = str(PurePosixPath(args.root) / DEFAULT_UPDATE_READY / "latest.json")
    if not upload_json(args.remote, update_latest_rel, update_latest):
        print("candidate_finalize_local_error code=update_ready_manifest_write_failed canonical_write=0")
        return 2
    if remote_json(args.remote, update_latest_rel) != update_latest:
        print("candidate_finalize_local_error code=update_ready_manifest_verify_failed canonical_write=0")
        return 2

    existing: dict[str, bytes | None] = {}
    for item in prepared_items:
        target = derive_target(
            record_type=item["record_type"],
            domain=item["domain"],
            category=item["category"],
            slug=item["slug"],
        )
        existing[target] = remote_bytes(args.remote, target)
    plan, validation_errors = assess_publication(
        prepared_items, prepared_contents, existing, master_text
    )
    if validation_errors or plan is None:
        print(
            "candidate_finalize_local_error "
            f"codes={','.join(validation_errors or ['publish_validation_failed'])} canonical_write=0"
        )
        return 2

    plan_rel = str(PurePosixPath(args.root) / DEFAULT_PUBLISH_OUTBOX / "dry-run-plan.json")
    if not upload_json(args.remote, plan_rel, plan):
        print("candidate_finalize_local_error code=dry_run_plan_write_failed canonical_write=0")
        return 2

    publish_manifest_raw = remote_bytes(args.remote, latest_rel)
    publish_validation_raw = remote_bytes(args.remote, plan_rel)
    update_manifest_raw = remote_bytes(args.remote, update_latest_rel)
    batch, batch_errors = build_finalize_batch(
        master_text.encode("utf-8"),
        publish_manifest_raw or b"",
        publish_validation_raw or b"",
        update_manifest_raw or b"",
    )
    if batch_errors or batch is None:
        print(
            "candidate_finalize_local_error "
            f"codes={','.join(batch_errors or ['finalize_batch_failed'])} canonical_write=0"
        )
        return 2
    batch_rel = str(PurePosixPath(args.root) / DEFAULT_FINALIZE_BATCH)
    if not write_finalize_batch_atomic(args.remote, batch_rel, batch, root=args.root):
        print("candidate_finalize_local_error code=finalize_batch_write_failed canonical_write=0")
        return 2
    if remote_json(args.remote, batch_rel) != batch:
        print("candidate_finalize_local_error code=finalize_batch_verify_failed canonical_write=0")
        return 2

    print(
        "candidate_finalize_local_ok "
        f"seen={len(names)} prepared={len(prepared_items)} updates_planned={update_planned} "
        f"updates_ready={updates_ready} held={held} source_refreshed={source_refreshed} "
        f"source_fallback={source_fallback} create={plan['counts']['create']} "
        f"unchanged={plan['counts']['unchanged']} finalize_batch={batch['batch_id']} "
        f"finalize_version={FINALIZE_VERSION} canonical_write=0 paid_model=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
