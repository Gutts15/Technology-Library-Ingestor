#!/usr/bin/env python3
"""Smoke tests for guarded rclone-backed remote recovery probe helpers."""

from __future__ import annotations

import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_remote_recovery_probe import (
    BEFORE_BYTES,
    PROBE_VERSION,
    build_prepared_journal,
    fixture_prefix,
    safe_fixture_id,
    sha256,
    validate_prepared_journal,
)


def main() -> None:
    fixture_id = "1234567890abcdef12345678"
    assert safe_fixture_id(fixture_id)
    assert not safe_fixture_id("bad")
    assert fixture_prefix(fixture_id).startswith("99_INBOX/CANDIDATES/RECOVERY_REMOTE_FIXTURE/")

    journal = build_prepared_journal(fixture_id)
    assert journal["schema_version"] == 1
    assert journal["probe_version"] == PROBE_VERSION == "0.1.0"
    assert journal["state"] == "PREPARED"
    assert journal["fixture_only"] is True
    assert journal["snapshot_sha256"] == sha256(BEFORE_BYTES)
    assert journal["target_before_sha256"] == sha256(BEFORE_BYTES)
    assert journal["rollback_verified"] is False
    assert journal["production_publish_authorized"] is False
    assert journal["canonical_write_performed"] is False
    assert validate_prepared_journal(journal, fixture_id) == []

    weak = dict(journal)
    weak["state"] = "RECOVERED"
    assert "journal_state" in validate_prepared_journal(weak, fixture_id)

    dirty = dict(journal)
    dirty["canonical_write_performed"] = True
    assert "journal_write_flag" in validate_prepared_journal(dirty, fixture_id)

    print(
        "candidate_remote_recovery_probe_smoke_ok private_fixture_root=1 prepared_contract=1 "
        "fail_closed=1 authorization_separate=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
