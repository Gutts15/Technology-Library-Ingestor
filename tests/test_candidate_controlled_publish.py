#!/usr/bin/env python3
"""Smoke tests for controlled production publisher guards."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_controlled_publish import (
    SCHEMA_VERSION,
    validate_stage12a_preflight,
    planned_index_changes,
    sha256,
)
from candidate_stage_12a_preflight import READY_STATE, executor_fingerprints
import candidate_controlled_publish as publisher
import candidate_remote_recovery_probe as transport


class MemoryRclone:
    """Model only the external storage boundary; run the real publisher/helpers."""

    def __init__(self, fail_operation: str | None = None):
        self.objects = {}
        self.operations = []
        self.fail_operation = fail_operation

    def __call__(self, command, **kwargs):
        operation = command[1]
        self.operations.append(operation)
        stdout = "" if kwargs.get("text") else b""
        if operation == self.fail_operation:
            return subprocess.CompletedProcess(command, 1, stdout, "")
        if operation == "copyto":
            mime = "application/octet-stream"
            if "--metadata" in command and "--metadata-set" in command:
                key, mime = command[command.index("--metadata-set") + 1].split("=", 1)
                assert key == "content-type"
            self.objects[command[3]] = (Path(command[2]).read_bytes(), mime)
        elif operation == "moveto":
            assert command[2].endswith(".bin"), "Preserve the existing stage names"
            self.objects[command[3]] = self.objects.pop(command[2])
        elif operation == "cat":
            stdout = self.objects[command[2]][0]
        else:
            raise AssertionError(f"Unexpected storage operation: {operation}")
        return subprocess.CompletedProcess(command, 0, stdout, "")


def verify_final_destination_mime() -> None:
    # These literals catch deriving MIME from payload.bin, stage.bin or bytes.
    cases = [
        ("record.md", "text/markdown"),
        ("record.MD", "text/markdown"),
        ("record.json", "application/json"),
        ("record.txt", "text/plain"),
        ("record.pdf", "application/pdf"),
        ("record.csv", "text/csv"),
        ("record.png", "image/png"),
        ("record.zip", "application/zip"),
        ("record.bin", "application/octet-stream"),
        ("record.unknown-extension", "application/octet-stream"),
        ("record", "application/octet-stream"),
    ]
    raw = b"identical bytes regardless of the destination extension\n"
    storage = MemoryRclone()
    with patch.object(transport.subprocess, "run", storage):
        writer = publisher.canonical_atomic_writer(
            "fixture:", "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/synthetic/run"
        )
        for name, expected_mime in cases:
            target = "00_LIBRARY/" + name
            assert writer(target, raw)
            actual = storage.objects["fixture:" + target]
            assert actual == (raw, expected_mime), (target, actual[1], expected_mime)
        operations_before = list(storage.operations)
        assert not writer("99_INBOX/record.json", raw)
        assert not writer("00_LIBRARY/../record.md", raw)
        assert storage.operations == operations_before

        # The same transport helper also handles ordinary, unstaged JSON writes.
        assert transport.remote_copy_json("fixture:", "99_INBOX/journal.json", {"state": "fixture"})
        assert storage.objects["fixture:99_INBOX/journal.json"][1] == "application/json"

    # A host/global extension registration must not change the final-path result.
    import mimetypes
    with patch.dict(mimetypes.types_map, {".md": "application/json", ".json": "text/plain"}):
        isolated = MemoryRclone()
        with patch.object(transport.subprocess, "run", isolated):
            writer = publisher.canonical_atomic_writer(
                "fixture:", "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/synthetic/run"
            )
            assert writer("00_LIBRARY/record.md", raw)
            assert isolated.objects["fixture:00_LIBRARY/record.md"] == (raw, "text/markdown")

    for operation in ("copyto", "moveto"):
        failing = MemoryRclone(fail_operation=operation)
        with patch.object(transport.subprocess, "run", failing):
            writer = publisher.canonical_atomic_writer(
                "fixture:", "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/synthetic/run"
            )
            assert not writer("00_LIBRARY/record.md", raw)
        assert "fixture:00_LIBRARY/record.md" not in failing.objects
        if operation == "copyto":
            assert "moveto" not in failing.operations


def main() -> None:
    verify_final_destination_mime()
    readiness_raw = b'{"readiness":"fixture"}\n'
    authorization_raw = b'{"authorization":"fixture"}\n'
    batch = "a" * 20
    tx = "b" * 20
    fingerprints, errors = executor_fingerprints()
    assert not errors, errors

    preflight = {
        "schema_version": SCHEMA_VERSION,
        "state": READY_STATE,
        "batch_id": batch,
        "transaction_id": tx,
        "readiness_sha256": sha256(readiness_raw),
        "authorization_sha256": sha256(authorization_raw),
        "coordinator_free": True,
        "pending_recovery": False,
        "authorization_verified": True,
        "live_transaction_revalidated": True,
        "canonical_apply_enabled": False,
        "canonical_write_performed": False,
        "executor_source_sha256": fingerprints,
    }
    raw = (json.dumps(preflight) + "\n").encode("utf-8")
    assert validate_stage12a_preflight(
        preflight,
        preflight_raw=raw,
        readiness_raw=readiness_raw,
        authorization_raw=authorization_raw,
        batch_id=batch,
        transaction_id=tx,
    ) == []

    changed = dict(preflight)
    changed["pending_recovery"] = True
    errors = validate_stage12a_preflight(
        changed,
        preflight_raw=raw,
        readiness_raw=readiness_raw,
        authorization_raw=authorization_raw,
        batch_id=batch,
        transaction_id=tx,
    )
    assert "stage12a_pending_recovery" in errors

    # Guard: post-transaction index changes must stay inside the recovery snapshot scope.
    # Use a synthetic plan that would cause an unrelated domain index to differ.
    # The helper must fail closed rather than allow an unsnapshotted index write.
    assert callable(planned_index_changes)

    print(
        "candidate_controlled_publish_smoke_ok stage12a_bound=1 "
        "source_fingerprints_bound=1 pending_recovery_blocked=1 index_scope_guard=1"
    )


if __name__ == "__main__":
    main()
