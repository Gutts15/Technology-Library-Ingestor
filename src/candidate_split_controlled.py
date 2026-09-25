#!/usr/bin/env python3
"""Prepare and explicitly apply a reviewed candidate split without canonical publication.

Planning mode re-runs the local semantic review for one READY_FOR_SEMANTIC
candidate, requires SPLIT_REQUIRED, builds the full child-candidate split plan,
and writes only a local JSON envelope. It never writes candidate children,
changes lifecycle state, touches 00_LIBRARY, or uses a paid/remote model.

Apply mode consumes that exact local envelope. It performs no model call. After
strict hash/schema/collision preflight, it writes the child candidates into the
private CHAT_RESEARCH queue, verifies their bytes, then moves the parent into a
private RESOLVED/SPLIT_PARENT bucket. It never writes to 00_LIBRARY.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_quality_benchmark import basename, resolve_selected
from candidate_queue import validate_candidate
from candidate_semantic_batch import normalize_candidate_text, remote_json, remote_text, review_one
from candidate_semantic_local import endpoint_is_loopback
from candidate_split_plan import (
    SPLIT_PLAN_VERSION,
    build_prompt as build_split_prompt,
    build_split_plan,
    call_ollama as call_split_ollama,
)

SCHEMA_VERSION = 1
CONTROLLED_SPLIT_VERSION = "0.1.0"
DEFAULT_ROOT = "99_INBOX/CANDIDATES"
DEFAULT_MASTER = "00_LIBRARY/MASTER_INDEX.md"
DEFAULT_OUT_DIR = Path("output/candidate-split-controlled")
DEFAULT_PARENT_ARCHIVE = "RESOLVED/SPLIT_PARENT"
DEFAULT_STATE_DIR = "SPLIT_APPLY_STATE"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["rclone", *arguments], check=False, capture_output=True)


def remote_bytes(remote: str, relative: str) -> bytes | None:
    result = run_rclone(["cat", join_remote(remote, relative), "--log-level", "ERROR"])
    return result.stdout if result.returncode == 0 else None


def normalized_sha(raw: bytes) -> str | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    normalized = normalize_candidate_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_local_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_remote_bytes(remote: str, relative: str, content: bytes) -> bool:
    parent = str(PurePosixPath(relative).parent)
    mkdir = run_rclone(["mkdir", join_remote(remote, parent), "--log-level", "ERROR"])
    if mkdir.returncode != 0:
        return False
    with tempfile.NamedTemporaryFile(prefix="tl-controlled-split-", delete=False) as handle:
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


def plan_path_for(candidate_name: str, out_dir: Path) -> Path:
    stem = PurePosixPath(candidate_name).stem
    return out_dir / f"{stem}.split-plan.json"


def build_envelope(
    *,
    candidate_name: str,
    candidate_path: str,
    candidate_id: str,
    candidate_sha: str,
    decision: dict[str, Any],
    split_plan: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "controlled_split_version": CONTROLLED_SPLIT_VERSION,
        "generated_at": utc_now(),
        "parent": {
            "candidate_name": candidate_name,
            "candidate_path": candidate_path,
            "candidate_id": candidate_id,
            "candidate_sha256": candidate_sha,
        },
        "semantic": {
            "decision": decision.get("decision"),
            "evidence_level": decision.get("evidence_level"),
        },
        "split_plan": split_plan,
        "candidate_write_performed": False,
        "parent_transition_performed": False,
        "canonical_write_performed": False,
        "paid_model_used": False,
    }


def validate_envelope(payload: dict[str, Any] | None) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return None, ["envelope_missing"]
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("envelope_schema")
    if payload.get("controlled_split_version") != CONTROLLED_SPLIT_VERSION:
        errors.append("envelope_version")
    parent = payload.get("parent")
    semantic = payload.get("semantic")
    plan = payload.get("split_plan")
    if not isinstance(parent, dict):
        errors.append("parent_missing")
    if not isinstance(semantic, dict) or semantic.get("decision") != "SPLIT_REQUIRED":
        errors.append("semantic_not_split_required")
    if not isinstance(plan, dict):
        errors.append("split_plan_missing")
    if errors:
        return None, sorted(set(errors))

    name = parent.get("candidate_name")
    path = parent.get("candidate_path")
    candidate_id = parent.get("candidate_id")
    candidate_sha = parent.get("candidate_sha256")
    if not all(isinstance(v, str) and v for v in (name, path, candidate_id, candidate_sha)):
        errors.append("parent_identity_invalid")
    if plan.get("split_plan_version") != SPLIT_PLAN_VERSION:
        errors.append("split_plan_version")
    if plan.get("parent_candidate_id") != candidate_id:
        errors.append("split_parent_id_mismatch")
    if plan.get("parent_candidate_sha256") != candidate_sha:
        errors.append("split_parent_sha_mismatch")
    if plan.get("candidate_write_performed") is not False:
        errors.append("unsafe_candidate_write_flag")
    if plan.get("canonical_write_performed") is not False:
        errors.append("unsafe_canonical_write_flag")
    if plan.get("paid_model_used") is not False:
        errors.append("unsafe_paid_model_flag")

    children = plan.get("children")
    if not isinstance(children, list) or not (2 <= len(children) <= 8):
        errors.append("child_count_invalid")
        children = []
    seen_names: set[str] = set()
    for index, child in enumerate(children):
        if not isinstance(child, dict):
            errors.append(f"child_{index}_invalid")
            continue
        filename = child.get("filename")
        child_id = child.get("candidate_id")
        child_sha = child.get("candidate_sha256")
        markdown = child.get("markdown")
        if not all(isinstance(v, str) and v for v in (filename, child_id, child_sha, markdown)):
            errors.append(f"child_{index}_identity")
            continue
        if filename in seen_names:
            errors.append(f"child_{index}_duplicate_filename")
        seen_names.add(filename)
        _, candidate_errors = validate_candidate(filename, markdown)
        if candidate_errors:
            errors.append(f"child_{index}_candidate_invalid")
        digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        if digest != child_sha or digest[:20] != child_id:
            errors.append(f"child_{index}_hash_mismatch")

    return (payload if not errors else None), sorted(set(errors))


def prepare_one(
    *,
    remote: str,
    root: str,
    candidate_name: str,
    model: str,
    endpoint: str,
    timeout: float,
    out_path: Path,
) -> tuple[dict[str, Any] | None, list[str]]:
    index = remote_json(remote, str(PurePosixPath(root) / "index.json"))
    proposals = remote_json(remote, str(PurePosixPath(root) / "validation-proposals.json"))
    master_text = remote_text(remote, DEFAULT_MASTER)
    if index is None or proposals is None or master_text is None:
        return None, ["private_state_missing"]
    entries, selection_errors = resolve_selected(index, proposals, [candidate_name])
    if selection_errors or len(entries) != 1:
        return None, selection_errors or ["selection_failed"]
    entry = entries[0]
    path = entry.get("path")
    candidate_id = entry.get("candidate_id")
    candidate_sha = entry.get("sha256")
    if not all(isinstance(v, str) and v for v in (path, candidate_id, candidate_sha)):
        return None, ["index_entry_invalid"]
    name = basename(path)
    candidate_text = remote_text(remote, str(PurePosixPath(root) / path))
    if candidate_text is None:
        return None, ["candidate_read_failed"]

    rich, decision = review_one(
        candidate_id=candidate_id,
        candidate_sha=candidate_sha,
        candidate_text=candidate_text,
        master_text=master_text,
        model=model,
        endpoint=endpoint,
        timeout=timeout,
    )
    if decision.get("decision") != "SPLIT_REQUIRED":
        return None, [f"semantic_not_split_required:{decision.get('decision')}"]
    probe = rich.get("source_probe") if isinstance(rich, dict) else None
    if not isinstance(probe, dict):
        return None, ["source_probe_missing"]
    payload = call_split_ollama(
        endpoint,
        model,
        build_split_prompt(candidate_text, rich, probe),
        timeout,
    )
    if payload is None:
        return None, ["local_split_model_failed"]
    split_plan, split_errors = build_split_plan(candidate_text, rich, payload)
    if split_errors or split_plan is None:
        return None, split_errors or ["split_plan_failed"]
    envelope = build_envelope(
        candidate_name=name,
        candidate_path=path,
        candidate_id=candidate_id,
        candidate_sha=candidate_sha,
        decision=decision,
        split_plan=split_plan,
    )
    validated, errors = validate_envelope(envelope)
    if errors or validated is None:
        return None, errors or ["envelope_invalid"]
    write_local_json(out_path, envelope)
    return envelope, []


def preflight_apply(
    *,
    envelope: dict[str, Any],
    remote: str,
    root: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    validated, errors = validate_envelope(envelope)
    if errors or validated is None:
        return None, errors or ["envelope_invalid"]
    parent = validated["parent"]
    plan = validated["split_plan"]
    parent_name = parent["candidate_name"]
    source_rel = str(PurePosixPath(root) / parent["candidate_path"])
    archive_rel = str(PurePosixPath(root) / DEFAULT_PARENT_ARCHIVE / parent_name)
    expected_parent_sha = parent["candidate_sha256"]

    source = remote_bytes(remote, source_rel)
    archive = remote_bytes(remote, archive_rel)
    source_sha = normalized_sha(source) if source is not None else None
    archive_sha = normalized_sha(archive) if archive is not None else None
    if source is None:
        if archive_sha != expected_parent_sha:
            errors.append("parent_source_missing")
    elif source_sha != expected_parent_sha:
        errors.append("parent_revision_changed")
    if archive is not None and archive_sha != expected_parent_sha:
        errors.append("parent_archive_conflict")

    child_actions: list[dict[str, Any]] = []
    for child in plan["children"]:
        destination = str(PurePosixPath(root) / "CHAT_RESEARCH" / child["filename"])
        existing = remote_bytes(remote, destination)
        existing_sha = normalized_sha(existing) if existing is not None else None
        if existing is not None and existing_sha != child["candidate_sha256"]:
            errors.append(f"child_conflict:{child['filename']}")
        child_actions.append(
            {
                "filename": child["filename"],
                "destination": destination,
                "candidate_sha256": child["candidate_sha256"],
                "markdown": child["markdown"],
                "already_present": existing_sha == child["candidate_sha256"],
            }
        )
    if errors:
        return None, sorted(set(errors))
    return {
        "parent_source": source_rel,
        "parent_archive": archive_rel,
        "parent_sha256": expected_parent_sha,
        "parent_already_archived": source is None and archive_sha == expected_parent_sha,
        "children": child_actions,
    }, []


def apply_private_split(
    *,
    envelope: dict[str, Any],
    remote: str,
    root: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    plan, errors = preflight_apply(envelope=envelope, remote=remote, root=root)
    if errors or plan is None:
        return None, errors or ["preflight_failed"]

    for child in plan["children"]:
        if child["already_present"]:
            continue
        content = child["markdown"].encode("utf-8")
        if not write_remote_bytes(remote, child["destination"], content):
            return None, [f"child_write_failed:{child['filename']}"]
        written = remote_bytes(remote, child["destination"])
        if written is None or normalized_sha(written) != child["candidate_sha256"]:
            return None, [f"child_verify_failed:{child['filename']}"]

    if not plan["parent_already_archived"]:
        mkdir = run_rclone([
            "mkdir",
            join_remote(remote, str(PurePosixPath(plan["parent_archive"]).parent)),
            "--log-level",
            "ERROR",
        ])
        if mkdir.returncode != 0:
            return None, ["parent_archive_directory_failed"]
        archive_existing = remote_bytes(remote, plan["parent_archive"])
        if archive_existing is None:
            move = run_rclone([
                "moveto",
                join_remote(remote, plan["parent_source"]),
                join_remote(remote, plan["parent_archive"]),
                "--log-level",
                "ERROR",
            ])
            if move.returncode != 0:
                return None, ["parent_move_failed"]
        else:
            cleanup = run_rclone([
                "deletefile",
                join_remote(remote, plan["parent_source"]),
                "--log-level",
                "ERROR",
            ])
            if cleanup.returncode != 0:
                return None, ["parent_source_cleanup_failed"]
        archived = remote_bytes(remote, plan["parent_archive"])
        if archived is None or normalized_sha(archived) != plan["parent_sha256"]:
            return None, ["parent_archive_verify_failed"]

    parent = envelope["parent"]
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "controlled_split_version": CONTROLLED_SPLIT_VERSION,
        "applied_at": utc_now(),
        "parent_candidate_id": parent["candidate_id"],
        "parent_candidate_sha256": parent["candidate_sha256"],
        "parent_destination_bucket": DEFAULT_PARENT_ARCHIVE,
        "children": [
            {
                "filename": child["filename"],
                "candidate_sha256": child["candidate_sha256"],
                "destination_bucket": "CHAT_RESEARCH",
            }
            for child in plan["children"]
        ],
        "candidate_write_performed": True,
        "parent_transition_performed": True,
        "canonical_write_performed": False,
        "paid_model_used": False,
    }
    state_rel = str(
        PurePosixPath(root) / DEFAULT_STATE_DIR / f"{parent['candidate_id']}.json"
    )
    if not write_remote_bytes(
        remote,
        state_rel,
        (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    ):
        return None, ["state_write_failed"]
    return receipt, []


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare or explicitly apply one controlled private candidate split."
    )
    parser.add_argument("--candidate", help="Candidate filename for planning mode.")
    parser.add_argument("--plan", type=Path, help="Existing local split envelope for apply/preflight mode.")
    parser.add_argument("--remote", default="tl:")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--model", default="gpt-oss:20b")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--apply", action="store_true", help="Write private split children and archive the parent. Never writes 00_LIBRARY.")
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("candidate_split_controlled_error code=rclone_missing canonical_write=0 paid_model=0")
        return 2

    timeout = max(10.0, min(float(args.timeout), 600.0))
    if args.apply or args.plan:
        if args.plan is None:
            print("candidate_split_controlled_error code=plan_required canonical_write=0 paid_model=0")
            return 2
        envelope = read_json(args.plan)
        validated, errors = validate_envelope(envelope)
        if errors or validated is None:
            print(f"candidate_split_controlled_error codes={','.join(errors or ['envelope_invalid'])} canonical_write=0 paid_model=0")
            return 2
        if not args.apply:
            preflight, preflight_errors = preflight_apply(envelope=validated, remote=args.remote, root=args.root)
            if preflight_errors or preflight is None:
                print(f"candidate_split_controlled_error codes={','.join(preflight_errors or ['preflight_failed'])} canonical_write=0 paid_model=0")
                return 2
            print(
                "candidate_split_controlled_ok mode=preflight "
                f"children={len(preflight['children'])} candidate_write=0 parent_transition=0 canonical_write=0 paid_model=0"
            )
            return 0
        receipt, apply_errors = apply_private_split(envelope=validated, remote=args.remote, root=args.root)
        if apply_errors or receipt is None:
            print(f"candidate_split_controlled_error codes={','.join(apply_errors or ['apply_failed'])} canonical_write=0 paid_model=0")
            return 2
        print(
            "candidate_split_controlled_ok mode=apply "
            f"children={len(receipt['children'])} candidate_write=1 parent_transition=1 canonical_write=0 paid_model=0"
        )
        return 0

    if not args.candidate:
        print("candidate_split_controlled_error code=candidate_required canonical_write=0 paid_model=0")
        return 2
    if not endpoint_is_loopback(args.endpoint):
        print("candidate_split_controlled_error code=non_loopback_model_endpoint canonical_write=0 paid_model=0")
        return 2
    out_path = plan_path_for(args.candidate, args.out_dir)
    envelope, errors = prepare_one(
        remote=args.remote,
        root=args.root,
        candidate_name=args.candidate,
        model=args.model,
        endpoint=args.endpoint,
        timeout=timeout,
        out_path=out_path,
    )
    if errors or envelope is None:
        print(f"candidate_split_controlled_error codes={','.join(errors or ['prepare_failed'])} canonical_write=0 paid_model=0")
        return 2
    print(
        "candidate_split_controlled_ok mode=prepare "
        f"children={envelope['split_plan']['child_count']} plan={out_path.as_posix()} "
        "candidate_write=0 parent_transition=0 canonical_write=0 paid_model=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
