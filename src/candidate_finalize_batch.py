#!/usr/bin/env python3
"""Bind finalizer control files into one deterministic private batch barrier.

The finalizer writes several private transaction inputs independently. A consumer
must not combine files from different runs during that small write window. This
module hashes the exact current MASTER_INDEX plus the exact bytes of the new-lane
manifest, new-lane dry-run plan and UPDATE_READY manifest, then emits one batch
identity written last.

The batch document is private coordination state. It never writes to 00_LIBRARY.
"""

from __future__ import annotations

from rclone_paths import join_remote

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
FINALIZE_BATCH_VERSION = "0.1.0"
DEFAULT_ROOT = "99_INBOX/CANDIDATES"
DEFAULT_BATCH = "FINALIZE_BATCH/current.json"
ID_LENGTH = 20


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def build_finalize_batch(
    master_index: bytes,
    publish_manifest: bytes,
    publish_validation: bytes,
    update_manifest: bytes,
) -> tuple[dict[str, Any] | None, list[str]]:
    payloads = {
        "master_index_sha256": master_index,
        "publish_manifest_sha256": publish_manifest,
        "publish_validation_sha256": publish_validation,
        "update_manifest_sha256": update_manifest,
    }
    errors = [name.replace("_sha256", "_missing") for name, raw in payloads.items() if not raw]
    if errors:
        return None, sorted(errors)
    hashes = {name: sha256(raw) for name, raw in payloads.items()}
    digest = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    batch_id = sha256(b"candidate-finalize-batch-v1|" + digest)[:ID_LENGTH]
    return {
        "schema_version": SCHEMA_VERSION,
        "finalize_batch_version": FINALIZE_BATCH_VERSION,
        "batch_id": batch_id,
        **hashes,
        "canonical_write_performed": False,
    }, []


def verify_finalize_batch(
    batch: dict[str, Any] | None,
    master_index: bytes,
    publish_manifest: bytes,
    publish_validation: bytes,
    update_manifest: bytes,
) -> list[str]:
    if not isinstance(batch, dict):
        return ["finalize_batch_missing"]
    errors: list[str] = []
    if batch.get("schema_version") != SCHEMA_VERSION:
        errors.append("finalize_batch_schema")
    if batch.get("finalize_batch_version") != FINALIZE_BATCH_VERSION:
        errors.append("finalize_batch_version")
    if batch.get("canonical_write_performed") is not False:
        errors.append("finalize_batch_write_flag")
    expected, build_errors = build_finalize_batch(
        master_index,
        publish_manifest,
        publish_validation,
        update_manifest,
    )
    if build_errors or expected is None:
        return sorted(set(errors + build_errors))
    for field in (
        "batch_id",
        "master_index_sha256",
        "publish_manifest_sha256",
        "publish_validation_sha256",
        "update_manifest_sha256",
    ):
        if batch.get(field) != expected.get(field):
            errors.append(f"finalize_batch_{field}_mismatch")
    return sorted(set(errors))


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["rclone", *arguments], check=False, capture_output=True)


def write_remote_atomic(
    remote: str,
    relative: str,
    payload: dict[str, Any],
    *,
    root: str = DEFAULT_ROOT,
) -> bool:
    if not shutil.which("rclone"):
        return False
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    batch_id = str(payload.get("batch_id") or "invalid")
    stage = str(PurePosixPath(root) / "FINALIZE_PREPARING" / f"{batch_id}.json")
    with tempfile.NamedTemporaryFile(prefix="candidate-finalize-batch-", delete=False) as handle:
        handle.write(data)
        local_name = handle.name
    try:
        for parent in (str(PurePosixPath(relative).parent), str(PurePosixPath(stage).parent)):
            if run_rclone(["mkdir", join_remote(remote, parent), "--log-level", "ERROR"]).returncode != 0:
                return False
        if run_rclone(["copyto", local_name, join_remote(remote, stage), "--log-level", "ERROR"]).returncode != 0:
            return False
        move = run_rclone([
            "moveto",
            join_remote(remote, stage),
            join_remote(remote, relative),
            "--log-level",
            "ERROR",
        ])
        return move.returncode == 0
    finally:
        try:
            Path(local_name).unlink()
        except OSError:
            pass
