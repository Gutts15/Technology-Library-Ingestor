#!/usr/bin/env python3
"""Stage 15 receipt-bound production candidate settlement.

The stage is fail-closed and may mutate only private candidate lifecycle state under
99_INBOX/CANDIDATES. It never writes, moves, or deletes anything under 00_LIBRARY.

One explicit apply performs all Stage 15 sub-stages:
15A derive exact settlement actions from the verified receipt + Stage 14 acceptance;
15B move published READY_FOR_CURATION candidates to RESOLVED/PUBLISHED/<transaction>;
15C archive candidate-scoped PUBLISH_READY / UPDATE_READY transient artifacts;
15D verify all moves byte-for-byte and persist a settlement receipt.

The operation is forward-idempotent: a rerun after a partial private move accepts an
already-present destination only when its bytes match exactly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_remote_recovery_probe import join_remote, remote_cat, run_rclone

SCHEMA_VERSION = 1
STAGE15_VERSION = "0.2.0"
CANDIDATE_ROOT = "99_INBOX/CANDIDATES"
READY = "READY_FOR_CURATION"
RESOLVED_PUBLISHED = "RESOLVED/PUBLISHED"
CURATION_DRAFTS = "CURATION_DRAFTS"
SETTLEMENTS = "SETTLEMENTS"
TRANSACTION_READY = "TRANSACTION_READY"
MAX_ITEMS = 50


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def remote_json(remote: str, relative: str) -> dict[str, Any] | None:
    raw = remote_cat(remote, relative)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _candidate_private(relative: str) -> str:
    path = PurePosixPath(relative.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("private_path_invalid")
    normalized = path.as_posix().strip("/")
    if not normalized.startswith(CANDIDATE_ROOT + "/"):
        raise ValueError("private_path_outside_candidate_root")
    if normalized.startswith("00_LIBRARY/") or "/00_LIBRARY/" in normalized:
        raise ValueError("canonical_path_forbidden")
    return normalized


def transaction_ready_paths(transaction_id: str) -> tuple[str, str]:
    source = f"{CANDIDATE_ROOT}/{TRANSACTION_READY}/plan.json"
    destination = (
        f"{CANDIDATE_ROOT}/{RESOLVED_PUBLISHED}/{transaction_id}/AUDIT/"
        f"{TRANSACTION_READY}/plan.json"
    )
    return source, destination


def validate_transaction_ready_payload(
    payload: dict[str, Any] | None,
    *,
    transaction_id: str,
    batch_id: str | None,
) -> list[str]:
    if not isinstance(payload, dict):
        return ["transaction_ready_plan_invalid"]
    errors: list[str] = []
    if payload.get("transaction_id") != transaction_id:
        errors.append("transaction_ready_transaction_mismatch")
    if payload.get("finalize_batch_id") != batch_id:
        errors.append("transaction_ready_batch_mismatch")
    return sorted(set(errors))


def list_remote_files(remote: str, relative: str, recursive: bool = False) -> tuple[list[str], list[str]]:
    args = ["lsjson", join_remote(remote, relative), "--files-only", "--log-level", "ERROR"]
    if recursive:
        args.insert(2, "--recursive")
    result = run_rclone(args)
    if result.returncode != 0:
        return [], [f"list_failed:{relative}"]
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return [], [f"list_invalid_json:{relative}"]
    if not isinstance(rows, list):
        return [], [f"list_invalid:{relative}"]
    paths: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get("Path") or row.get("Name")
        if isinstance(value, str):
            paths.append(value.replace("\\", "/").strip("/"))
    return sorted(set(paths)), []


def load_curation_packages(remote: str) -> tuple[list[dict[str, Any]], list[str]]:
    base = f"{CANDIDATE_ROOT}/{CURATION_DRAFTS}"
    paths, errors = list_remote_files(remote, base, recursive=True)
    if errors:
        return [], errors
    packages: list[dict[str, Any]] = []
    for rel in paths:
        if not rel.endswith("/draft.json"):
            continue
        payload = remote_json(remote, f"{base}/{rel}")
        if not isinstance(payload, dict):
            errors.append(f"curation_draft_invalid:{rel}")
            continue
        packages.append(payload)
    return packages, sorted(set(errors))


def candidate_locations(remote: str, transaction_id: str) -> tuple[dict[str, dict[str, str]], list[str]]:
    locations: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    buckets = [
        f"{CANDIDATE_ROOT}/{READY}",
        f"{CANDIDATE_ROOT}/{RESOLVED_PUBLISHED}/{transaction_id}",
    ]
    for bucket in buckets:
        paths, list_errors = list_remote_files(remote, bucket, recursive=False)
        if list_errors:
            # The transaction destination may not exist before first apply.
            if bucket.endswith(transaction_id):
                continue
            errors.extend(list_errors)
            continue
        for name in paths:
            leaf = PurePosixPath(name).name
            if not leaf.startswith("candidate-") or not leaf.endswith(".md"):
                continue
            logical = f"{bucket}/{leaf}"
            raw = remote_cat(remote, logical)
            if raw is None:
                errors.append(f"candidate_read_failed:{logical}")
                continue
            digest = sha256(raw)
            candidate_id = digest[:20]
            current = locations.get(candidate_id)
            if current and current["sha256"] != digest:
                errors.append(f"candidate_location_conflict:{candidate_id}")
                continue
            if current and current["path"] != logical:
                # Source + exact destination may coexist after an interrupted cleanup.
                if current["sha256"] == digest:
                    current["alternate_path"] = logical
                    continue
            locations[candidate_id] = {"path": logical, "sha256": digest, "name": leaf}
    return locations, sorted(set(errors))


def _receipt_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("record_id")),
        str(item.get("target_path")),
        str(item.get("content_sha256")),
    )


def build_settlement_plan(
    remote: str,
    stage14: dict[str, Any],
    receipt: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if stage14.get("state") != "PASS":
        errors.append("stage14_state")
    if stage14.get("post_publication_accepted") is not True:
        errors.append("stage14_not_accepted")
    if stage14.get("candidate_settlement_allowed") is not True:
        errors.append("stage14_settlement_blocked")
    if stage14.get("canonical_write_performed") is not False:
        errors.append("stage14_write_flag")
    if receipt.get("state") != "VERIFIED_COMMITTED_STATE":
        errors.append("receipt_state")
    if receipt.get("candidate_settlement_eligible") is not True:
        errors.append("receipt_not_settlement_eligible")
    if receipt.get("canonical_write_performed") is not False:
        errors.append("receipt_write_flag")

    transaction_id = receipt.get("transaction_id")
    if not isinstance(transaction_id, str) or len(transaction_id) != 20:
        errors.append("transaction_id")
        transaction_id = ""
    if stage14.get("transaction_id") != transaction_id:
        errors.append("stage14_transaction_mismatch")

    receipt_items = receipt.get("items")
    verified_targets = stage14.get("verified_targets")
    if not isinstance(receipt_items, list) or not receipt_items or len(receipt_items) > MAX_ITEMS:
        errors.append("receipt_items")
        receipt_items = []
    if not isinstance(verified_targets, list):
        errors.append("stage14_verified_targets")
        verified_targets = []

    receipt_keys = {_receipt_key(item) for item in receipt_items if isinstance(item, dict)}
    stage14_keys = {_receipt_key(item) for item in verified_targets if isinstance(item, dict)}
    if receipt_keys != stage14_keys:
        errors.append("stage14_receipt_target_set_mismatch")

    packages, package_errors = load_curation_packages(remote)
    errors.extend(package_errors)
    by_candidate: dict[str, dict[str, Any]] = {}
    by_revision: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for index, package in enumerate(packages):
        candidate_id = package.get("candidate_id")
        candidate_sha = package.get("candidate_sha256")
        package_id = package.get("package_id")
        revision_key = package.get("revision_key")
        if not all(isinstance(v, str) and v for v in (candidate_id, candidate_sha, package_id, revision_key)):
            errors.append(f"curation_package_fields:{index}")
            continue
        if candidate_id in by_candidate and by_candidate[candidate_id] != package:
            errors.append(f"duplicate_curation_candidate:{candidate_id}")
        by_candidate[candidate_id] = package
        by_revision.setdefault((package_id, revision_key), []).append(package)

    locations, location_errors = candidate_locations(remote, transaction_id)
    errors.extend(location_errors)

    items: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    for index, item in enumerate(receipt_items):
        if not isinstance(item, dict):
            errors.append(f"receipt_item_invalid:{index}")
            continue
        lane = item.get("lane")
        action = item.get("action")
        record_id = item.get("record_id")
        target = item.get("target_path")
        content_sha = item.get("content_sha256")
        if lane == "NEW":
            package_id = item.get("package_id")
            revision_key = item.get("revision_key")
            matches = by_revision.get((str(package_id), str(revision_key)), [])
            if len(matches) != 1:
                errors.append(f"curation_revision_match:{index}:{len(matches)}")
                continue
            package = matches[0]
            if package.get("decision") not in {"VALIDATED_NEW", "SOURCE_ONLY"}:
                errors.append(f"curation_decision_mismatch:{index}")
        elif lane == "UPDATE":
            candidate_id = item.get("candidate_id")
            package = by_candidate.get(str(candidate_id))
            if package is None:
                errors.append(f"curation_candidate_missing:{index}")
                continue
            if package.get("decision") != "VALIDATED_UPDATE":
                errors.append(f"curation_decision_mismatch:{index}")
        else:
            errors.append(f"receipt_lane:{index}")
            continue

        candidate_id = str(package.get("candidate_id"))
        candidate_sha = str(package.get("candidate_sha256"))
        if candidate_id in seen_candidates:
            errors.append(f"duplicate_settlement_candidate:{candidate_id}")
            continue
        seen_candidates.add(candidate_id)

        proposed = package.get("proposed_record")
        if not isinstance(proposed, dict) or proposed.get("target_path") != target:
            errors.append(f"curation_target_mismatch:{index}")
            continue
        if lane == "NEW" and proposed.get("record_id") != record_id:
            errors.append(f"curation_record_id_mismatch:{index}")
            continue

        location = locations.get(candidate_id)
        if not isinstance(location, dict) or location.get("sha256") != candidate_sha:
            errors.append(f"candidate_location_missing_or_changed:{candidate_id}")
            continue

        filename = str(location["name"])
        source = f"{CANDIDATE_ROOT}/{READY}/{filename}"
        destination = f"{CANDIDATE_ROOT}/{RESOLVED_PUBLISHED}/{transaction_id}/{filename}"
        source_raw = remote_cat(remote, source)
        destination_raw = remote_cat(remote, destination)
        if source_raw is None and (destination_raw is None or sha256(destination_raw) != candidate_sha):
            errors.append(f"candidate_source_and_destination_missing:{candidate_id}")
            continue
        if source_raw is not None and sha256(source_raw) != candidate_sha:
            errors.append(f"candidate_source_sha_mismatch:{candidate_id}")
            continue
        if destination_raw is not None and sha256(destination_raw) != candidate_sha:
            errors.append(f"candidate_destination_conflict:{candidate_id}")
            continue

        target_raw = remote_cat(remote, str(target))
        if target_raw is None or sha256(target_raw) != content_sha:
            errors.append(f"canonical_target_changed:{target}")
            continue

        if lane == "NEW":
            transient_sources = [
                f"{CANDIDATE_ROOT}/PUBLISH_READY/candidate-{candidate_id}.json",
                f"{CANDIDATE_ROOT}/PUBLISH_READY/records/{record_id}.md",
            ]
        else:
            transient_sources = [
                f"{CANDIDATE_ROOT}/UPDATE_READY/{candidate_id}.json",
                f"{CANDIDATE_ROOT}/UPDATE_READY/records/{record_id}.md",
            ]

        transients: list[dict[str, str]] = []
        for transient_source in transient_sources:
            transient_destination = (
                f"{CANDIDATE_ROOT}/{RESOLVED_PUBLISHED}/{transaction_id}/AUDIT/"
                + transient_source.removeprefix(CANDIDATE_ROOT + "/")
            )
            source_bytes = remote_cat(remote, transient_source)
            destination_bytes = remote_cat(remote, transient_destination)
            if source_bytes is None and destination_bytes is None:
                errors.append(f"transient_missing:{transient_source}")
                continue
            expected = source_bytes if source_bytes is not None else destination_bytes
            assert expected is not None
            expected_sha = sha256(expected)
            if source_bytes is not None and sha256(source_bytes) != expected_sha:
                errors.append(f"transient_source_sha:{transient_source}")
            if destination_bytes is not None and sha256(destination_bytes) != expected_sha:
                errors.append(f"transient_destination_conflict:{transient_destination}")
            transients.append(
                {
                    "source_path": transient_source,
                    "destination_path": transient_destination,
                    "sha256": expected_sha,
                }
            )

        items.append(
            {
                "candidate_id": candidate_id,
                "candidate_sha256": candidate_sha,
                "decision": package.get("decision"),
                "lane": lane,
                "action": action,
                "record_id": record_id,
                "target_path": target,
                "content_sha256": content_sha,
                "source_path": source,
                "destination_path": destination,
                "transients": transients,
            }
        )

    if len(items) != len(receipt_items):
        errors.append(f"settlement_item_count:{len(items)}:{len(receipt_items)}")

    transaction_ready_source, transaction_ready_destination = transaction_ready_paths(transaction_id)
    transaction_ready_source_raw = remote_cat(remote, transaction_ready_source)
    transaction_ready_destination_raw = remote_cat(remote, transaction_ready_destination)
    if transaction_ready_source_raw is None and transaction_ready_destination_raw is None:
        errors.append("transaction_ready_plan_missing")
        transaction_ready_artifact = None
    else:
        transaction_ready_expected = (
            transaction_ready_source_raw
            if transaction_ready_source_raw is not None
            else transaction_ready_destination_raw
        )
        assert transaction_ready_expected is not None
        transaction_ready_sha = sha256(transaction_ready_expected)
        if (
            transaction_ready_source_raw is not None
            and transaction_ready_destination_raw is not None
            and transaction_ready_source_raw != transaction_ready_destination_raw
        ):
            errors.append("transaction_ready_destination_conflict")
        try:
            transaction_ready_payload = json.loads(transaction_ready_expected.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            transaction_ready_payload = None
            errors.append("transaction_ready_plan_invalid")
        errors.extend(
            validate_transaction_ready_payload(
                transaction_ready_payload if isinstance(transaction_ready_payload, dict) else None,
                transaction_id=transaction_id,
                batch_id=stage14.get("batch_id") if isinstance(stage14.get("batch_id"), str) else None,
            )
        )
        transaction_ready_artifact = {
            "source_path": transaction_ready_source,
            "destination_path": transaction_ready_destination,
            "sha256": transaction_ready_sha,
        }

    if errors:
        return None, sorted(set(errors))

    assert transaction_ready_artifact is not None
    ordered = sorted(items, key=lambda row: (str(row["candidate_id"]), str(row["target_path"])))
    return {
        "schema_version": SCHEMA_VERSION,
        "stage15_version": STAGE15_VERSION,
        "state": "READY_FOR_PRIVATE_SETTLEMENT",
        "transaction_id": transaction_id,
        "batch_id": stage14.get("batch_id"),
        "canonical_publication_verified": True,
        "stage14_acceptance_verified": True,
        "items": ordered,
        "transaction_ready": transaction_ready_artifact,
        "counts": {
            "candidates": len(ordered),
            "transients": sum(len(row["transients"]) for row in ordered) + 1,
            "create": sum(1 for row in ordered if row["action"] == "CREATE"),
            "update": sum(1 for row in ordered if row["action"] == "UPDATE"),
            "unchanged": sum(1 for row in ordered if row["action"] == "UNCHANGED"),
        },
        "private_lifecycle_write_performed": False,
        "canonical_write_performed": False,
    }, []


def _private_move_exact(remote: str, source: str, destination: str, expected_sha: str) -> tuple[bool, str | None]:
    try:
        source = _candidate_private(source)
        destination = _candidate_private(destination)
    except ValueError as exc:
        return False, str(exc)

    source_raw = remote_cat(remote, source)
    destination_raw = remote_cat(remote, destination)
    if source_raw is None:
        if destination_raw is not None and sha256(destination_raw) == expected_sha:
            return True, None
        return False, "source_missing"
    if sha256(source_raw) != expected_sha:
        return False, "source_sha_changed"
    if destination_raw is not None:
        if sha256(destination_raw) != expected_sha:
            return False, "destination_conflict"
        result = run_rclone(["deletefile", join_remote(remote, source), "--log-level", "ERROR"])
        return result.returncode == 0, None if result.returncode == 0 else "source_cleanup_failed"

    parent = str(PurePosixPath(destination).parent)
    mkdir = run_rclone(["mkdir", join_remote(remote, parent), "--log-level", "ERROR"])
    if mkdir.returncode != 0:
        return False, "destination_mkdir_failed"
    move = run_rclone(
        ["moveto", join_remote(remote, source), join_remote(remote, destination), "--log-level", "ERROR"]
    )
    if move.returncode != 0:
        return False, "move_failed"
    written = remote_cat(remote, destination)
    if written is None or sha256(written) != expected_sha:
        return False, "destination_verify_failed"
    return True, None


def persist_settlement_state(remote: str, state: dict[str, Any]) -> tuple[str | None, str | None]:
    transaction_id = str(state["transaction_id"])
    relative = f"{CANDIDATE_ROOT}/{SETTLEMENTS}/{transaction_id}.json"
    try:
        relative = _candidate_private(relative)
    except ValueError as exc:
        return None, str(exc)
    raw = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(prefix="candidate-settlement-", delete=False) as handle:
        handle.write(raw)
        local_name = handle.name
    try:
        parent = str(PurePosixPath(relative).parent)
        if run_rclone(["mkdir", join_remote(remote, parent), "--log-level", "ERROR"]).returncode != 0:
            return None, "settlement_state_mkdir_failed"
        result = run_rclone(["copyto", local_name, join_remote(remote, relative), "--log-level", "ERROR"])
        if result.returncode != 0:
            return None, "settlement_state_write_failed"
        readback = remote_cat(remote, relative)
        if readback != raw:
            return None, "settlement_state_readback_failed"
        return relative, None
    finally:
        try:
            Path(local_name).unlink()
        except OSError:
            pass


def apply_and_verify(remote: str, plan: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    moved_candidates = 0
    archived_transients = 0

    for item in plan["items"]:
        ok, error = _private_move_exact(
            remote,
            str(item["source_path"]),
            str(item["destination_path"]),
            str(item["candidate_sha256"]),
        )
        if not ok:
            errors.append(f"candidate_move:{item['candidate_id']}:{error}")
            break
        moved_candidates += 1

        for transient in item["transients"]:
            ok, error = _private_move_exact(
                remote,
                str(transient["source_path"]),
                str(transient["destination_path"]),
                str(transient["sha256"]),
            )
            if not ok:
                errors.append(f"transient_archive:{item['candidate_id']}:{error}")
                break
            archived_transients += 1
        if errors:
            break

    if not errors:
        transaction_ready = plan.get("transaction_ready")
        if not isinstance(transaction_ready, dict):
            errors.append("transaction_ready_plan_missing")
        else:
            ok, error = _private_move_exact(
                remote,
                str(transaction_ready["source_path"]),
                str(transaction_ready["destination_path"]),
                str(transaction_ready["sha256"]),
            )
            if not ok:
                errors.append(f"transaction_ready_archive:{error}")
            else:
                archived_transients += 1

    if errors:
        return None, errors

    for item in plan["items"]:
        source = remote_cat(remote, str(item["source_path"]))
        destination = remote_cat(remote, str(item["destination_path"]))
        if source is not None:
            errors.append(f"candidate_source_still_present:{item['candidate_id']}")
        if destination is None or sha256(destination) != item["candidate_sha256"]:
            errors.append(f"candidate_destination_verify:{item['candidate_id']}")
        canonical = remote_cat(remote, str(item["target_path"]))
        if canonical is None or sha256(canonical) != item["content_sha256"]:
            errors.append(f"canonical_target_post_settlement:{item['target_path']}")
        for transient in item["transients"]:
            transient_source = remote_cat(remote, str(transient["source_path"]))
            transient_destination = remote_cat(remote, str(transient["destination_path"]))
            if transient_source is not None:
                errors.append(f"transient_source_still_present:{transient['source_path']}")
            if transient_destination is None or sha256(transient_destination) != transient["sha256"]:
                errors.append(f"transient_destination_verify:{transient['destination_path']}")

    transaction_ready = plan.get("transaction_ready")
    if not isinstance(transaction_ready, dict):
        errors.append("transaction_ready_plan_missing")
    else:
        transaction_ready_source = remote_cat(remote, str(transaction_ready["source_path"]))
        transaction_ready_destination = remote_cat(remote, str(transaction_ready["destination_path"]))
        if transaction_ready_source is not None:
            errors.append("transaction_ready_source_still_present")
        if (
            transaction_ready_destination is None
            or sha256(transaction_ready_destination) != transaction_ready["sha256"]
        ):
            errors.append("transaction_ready_destination_verify")

    if errors:
        return None, sorted(set(errors))

    state = {
        **plan,
        "state": "SETTLED",
        "private_lifecycle_write_performed": True,
        "canonical_write_performed": False,
        "moves_verified": True,
        "transient_cleanup_verified": True,
        "canonical_targets_reverified": True,
        "accepted_candidates_pending": 0,
        "moved_candidates": moved_candidates,
        "archived_transients": archived_transients,
    }
    state_path, state_error = persist_settlement_state(remote, state)
    if state_error:
        return None, [state_error]
    state["settlement_state_path"] = state_path
    return state, []


def main() -> int:
    parser = argparse.ArgumentParser(description="Run receipt-bound Stage 15 candidate settlement.")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--stage14-report", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--apply-private-lifecycle", action="store_true")
    parser.add_argument("--confirm-transaction")
    parser.add_argument("--out", type=Path, default=Path("candidate-stage-15-settlement.json"))
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("candidate_stage_15_settlement_error code=rclone_missing canonical_write=0")
        return 2
    stage14 = read_json(args.stage14_report)
    receipt = read_json(args.receipt)
    if stage14 is None or receipt is None:
        print("candidate_stage_15_settlement_error code=evidence_read_failed canonical_write=0")
        return 2

    plan, errors = build_settlement_plan(str(args.remote), stage14, receipt)
    if errors or plan is None:
        print(
            "candidate_stage_15_settlement_error "
            f"codes={','.join(errors or ['settlement_plan_failed'])} canonical_write=0"
        )
        return 3

    if not args.apply_private_lifecycle:
        print(
            "candidate_stage_15_settlement_ok mode=plan "
            f"transaction_id={plan['transaction_id']} candidates={plan['counts']['candidates']} "
            f"transients={plan['counts']['transients']} settlement_allowed=1 "
            "private_lifecycle_write=0 canonical_write=0"
        )
        return 0

    if args.confirm_transaction != plan["transaction_id"]:
        print("candidate_stage_15_settlement_error code=transaction_confirmation_mismatch canonical_write=0")
        return 3

    state, apply_errors = apply_and_verify(str(args.remote), plan)
    if apply_errors or state is None:
        print(
            "candidate_stage_15_settlement_error "
            f"codes={','.join(apply_errors or ['settlement_apply_failed'])} canonical_write=0"
        )
        return 4

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_stage_15_settlement_ok mode=apply state=SETTLED "
        f"transaction_id={state['transaction_id']} candidates={state['counts']['candidates']} "
        f"transients={state['counts']['transients']} moves_verified=1 cleanup_verified=1 "
        "pending=0 private_lifecycle_write=1 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
