#!/usr/bin/env python3
"""E3 full-commit fixture over real private Drive, with all writes remapped.

This is a TEST HARNESS, not a general-purpose publisher. It executes the
existing E3 coordinator+commit engine over synthetic local data, but each
durable mutation is synchronously replicated and read back from a unique
private Drive fixture directory. Any uncertain remote acknowledgement exits
the runner without retry, rollback or coordinator release.

No real 00_LIBRARY, LOTES or DROP_HERE path may be modified. A local bare Git
coordinator remains part of the fixture; the production GitHub CAS lease is a
separate release gate.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from candidate_remote_recovery_probe import (
    join_remote, remote_cat, remote_copy_bytes, run_rclone,
)
import e3_recovery_fixture as recovery
import e3_commit_fixture as commit
from e3_lote_state import related_paths, verify_settlement
from e1_lote_prepare import LOTES, encode
from tests.test_e3_transaction_preflight import E3PreflightTests

PREFIX = "99_INBOX/CANDIDATES/E3_COMMITTED_REMOTE_FIXTURE"


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def raise_uncertain():
    # A remote timeout may complete after the call returns; never roll back
    # a remote state from here or free an uncertain writer.
    raise recovery.FixtureRecoveryError("fixture_remote_uncertain")


def synthetic_two_item_batch(case: E3PreflightTests):
    create = case.create_row()
    extra = dict(case.item)
    extra["package_id"] = "b" * 20
    extra["revision_key"] = "c" * 20
    summary = "React Bits provides animated React components. A standalone export workflow is now supported."
    extra["evidence_sha256"] = sha(summary.encode())
    extra["item_id"] = sha(
        ("e1|" + extra["package_id"] + "|" + extra["revision_key"] + "|" + extra["evidence_sha256"]).encode()
    )[:20]
    extra["evidence_compact"] = {"evidence": {"samples": [summary]}}
    extra["matches"] = [{"record_id": "0123456789abcdef0123", "title": "React Bits"}]
    case.lote["itens"] = [case.item, extra]
    case.lote["items_sha256"] = sha(encode(case.lote["itens"]))
    lot_raw = encode(case.lote)
    update = case.decision(
        "UPDATE", motivo=None, confianca=0.95,
        record_type="TECHNOLOGY", route="02_WEB_DEVELOPMENT/UI_COMPONENTS",
        target_record_id="0123456789abcdef0123",
        record_markdown=case.original.decode(),
        claims=[{"text": "Standalone export workflow supported",
                 "evidence_quote": "standalone export workflow"}])
    update["item_id"] = extra["item_id"]
    raw_decisions = encode({
        "schema_version": 1, "lote_id": case.lote["lote_id"],
        "lote_sha256": sha(lot_raw), "model": "gemini-3.5-flash-lite",
        "canonical_write_performed": False, "decisoes": [create, update]})
    return lot_raw, raw_decisions


def run_fixture(remote: str = "tl:") -> dict:
    if remote != "tl:":
        raise ValueError("fixture_remote_must_be_default")
    token = uuid.uuid4().hex[:24]
    prefix = PREFIX + "/" + token
    case = E3PreflightTests("test_no_write_decision_does_not_touch_canonical")
    case.setUp()
    root = case.root
    local_sync = recovery.sync_write
    writes = 0
    verified = 0
    success = False

    def remote_path(file: Path) -> str:
        resolved = file.resolve()
        if root.resolve() not in resolved.parents:
            raise ValueError("fixture_outside_root")
        relative = resolved.relative_to(root.resolve()).as_posix()
        if (not relative or relative.startswith("/") or ".." in Path(relative).parts
                or "\\" in relative or "//" in relative):
            raise ValueError("unsafe_fixture_relative")
        return prefix + "/" + relative

    def ship(file: Path, raw: bytes, *, must_absent: bool = False):
        nonlocal writes, verified
        relative = remote_path(file)
        # The prefix is randomly generated; pre-existence means collision.
        if must_absent and remote_cat(remote, relative) is not None:
            raise ValueError("fixture_file_collision")
        if not remote_copy_bytes(remote, relative, raw, final_path=file.name):
            raise_uncertain()
        writes += 1
        if remote_cat(remote, relative) != raw:
            raise_uncertain()
        verified += 1

    def sync_and_ship(file: Path, raw: bytes, *, new_only=False):
        local_sync(file, raw, new_only=new_only)
        ship(file, raw, must_absent=new_only)

    try:
        case.write(recovery.MARKER, recovery.MARKER_BYTES)
        bare = root / "coord.git"
        subprocess.run(["git", "init", "--bare", str(bare)],
                       check=True, capture_output=True, timeout=20)
        lot_raw, decisions_raw = synthetic_two_item_batch(case)
        lot_file = root / LOTES / case.path
        lot_file.write_bytes(lot_raw)
        decision_file = root / LOTES / "2026-10-27/decisoes.json"
        decision_file.parent.mkdir(parents=True, exist_ok=True)
        decision_file.write_bytes(decisions_raw)
        baseline = [p for p in (root / "00_LIBRARY").rglob("*") if p.is_file()]
        baseline.extend([lot_file, decision_file])
        for file in baseline:
            ship(file, file.read_bytes(), must_absent=True)
        # Every subsequent state change (canonical, index, journal, snapshot,
        # receipt, terminal sidecar) is verified in real private Drive.
        with patch.object(recovery, "sync_write", side_effect=sync_and_ship), \
             patch.object(commit, "sync_write", side_effect=sync_and_ship):
            result, tx = commit.exercise_commit(
                root, bare, lot_raw, case.path, decisions_raw)
        if result != "COMMITTED_RELEASED":
            raise ValueError("commit_incomplete")
        created = root / "00_LIBRARY/02_WEB_DEVELOPMENT/UI_COMPONENTS/technology-pixel-rig-tool.md"
        updated = root / case.canonical_path
        master = root / "00_LIBRARY/MASTER_INDEX.md"
        journal = recovery.load_journal(root, tx)
        if journal.get("state") != "COMMITTED":
            raise ValueError("journal_not_committed")
        if not updated.read_bytes().startswith(case.original.strip()):
            raise ValueError("existing_knowledge_changed")
        for path in [created, updated, master,
                     root / journal["run_root"] / "journal.json",
                     root / journal["run_root"] / "receipt.json",
                     root / LOTES / related_paths(case.path)[1]]:
            if remote_cat(remote, remote_path(path)) != path.read_bytes():
                raise ValueError("post_commit_drive_mismatch")
        receipt = (root / LOTES / related_paths(case.path)[1]).read_bytes()
        if verify_settlement(lot_raw, case.path, decisions_raw, receipt)["writes"] != 2:
            raise ValueError("terminal_outcomes_mismatch")
        success = True
    finally:
        if success:
            # Cleaning ONLY the unique synthetic prefix is safe after a
            # confirmed commit+release and byte-for-byte remote readback.
            deleted = run_rclone(["purge", join_remote(remote, prefix),
                                  "--log-level", "ERROR", "--stats", "0"])
            if deleted.returncode != 0:
                success = False
            else:
                listing = run_rclone(["lsjson", join_remote(remote, prefix),
                                      "--log-level", "ERROR"])
                # rclone reports a missing folder as a nonzero status; that's
                # also a successful deletion after prior readback.
                if listing.returncode == 0:
                    try:
                        success = json.loads(listing.stdout or "[]") == []
                    except ValueError:
                        success = False
        case.doCleanups()
    if not success:
        raise ValueError("fixture_cleanup_unverified")
    return {"status": "PASS", "create": 1, "update": 1,
            "terminal": 1, "journal": 1, "remote_writes": writes,
            "readback_verified": verified, "cleanup": 1,
            "real_canonical_write": 0}


def main() -> int:
    try:
        report = run_fixture()
    except (Exception, SystemExit):
        # Intentionally do not reveal remote paths, credentials, or content.
        print("e3_remote_commit status=BLOCKED canonical_write=0")
        return 2
    print("e3_remote_commit status=PASS create=1 update=1 "
          "receipt=1 journal=1 readback=1 cleanup=1 canonical_write=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
