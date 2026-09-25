#!/usr/bin/env python3
"""Dependency-free tests for READY -> FILE_EVIDENCE mechanical convergence."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ready_evidence_bridge import (
    DEFAULT_DESTINATION,
    build_envelope,
    build_index,
    expected_evidence_path,
    main,
    parse_handoff,
    semantic_summary,
)


def handoff_item(package_id: str, revision: str, kind: str = "document") -> dict:
    return {
        "package_id": package_id,
        "revision_key": revision,
        "kind": kind,
        "pipeline_version": "0.1.0",
        "compactor_version": "0.1.0",
        "evidence_bytes": 512,
        "evidence_path": expected_evidence_path(package_id),
        "detail_path": f"99_INBOX/READY_FOR_ANALYSIS/{package_id}/content-index.json",
        "preview_path": None,
    }


def main_test() -> None:
    package_id = "a" * 20
    revision = "b" * 20
    handoff = {
        "schema_version": 1,
        "handoff_version": "0.1.0",
        "items": [handoff_item(package_id, revision)],
    }
    parsed, errors = parse_handoff(handoff, 50)
    assert not errors, errors
    assert len(parsed) == 1

    summary = {
        "schema_version": 1,
        "summary_version": "0.1.0",
        "source": {
            "provider": "google_drive",
            "file_id": "private-provider-id",
            "sha256": "f" * 64,
        },
        "content": {"type": "pdf", "characters": 1200, "chunks": 1},
        "extraction": {"method": "pdftotext-layout", "pages_estimate": 2},
        "evidence": {"samples": ["Reusable technical sample."]},
        "audit": {"manifest": "ingest.json", "chunks": "chunks/"},
    }
    raw = (json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    envelope, envelope_errors = build_envelope(parsed[0], raw)
    assert not envelope_errors, envelope_errors
    assert envelope is not None
    assert envelope["type"] == "FILE_EVIDENCE_ENVELOPE"
    assert envelope["state"] == "READY_FOR_SEMANTIC_EXTRACTION"
    assert envelope["candidate_created"] is False
    assert envelope["canonical_write_performed"] is False
    assert envelope["curation_transition_performed"] is False
    assert envelope["evidence_summary_sha256"] == hashlib.sha256(raw).hexdigest()
    serialized = json.dumps(envelope, ensure_ascii=False)
    assert "private-provider-id" not in serialized
    assert "google_drive" not in serialized
    assert '"source"' not in json.dumps(envelope["semantic_summary"])
    assert '"audit"' not in json.dumps(envelope["semantic_summary"])
    assert "Reusable technical sample." in serialized

    link_summary = {
        "schema_version": 1,
        "summary_version": "0.1.0",
        "source": {"provider": "external", "file_id": "secret-id"},
        "link": {
            "url_without_query_or_fragment": "https://example.com/docs",
            "scheme": "https",
            "host": "example.com",
            "has_query": True,
            "has_fragment": False,
        },
        "network": {"fetched": False, "policy": "offline_v1"},
        "audit": {"manifest": "ingest.json"},
    }
    semantic, semantic_errors = semantic_summary(link_summary)
    assert not semantic_errors, semantic_errors
    assert semantic is not None
    assert "source" not in semantic and "audit" not in semantic
    assert semantic["link"]["host"] == "example.com"

    bad_handoff = {
        "schema_version": 1,
        "items": [
            {
                **handoff_item(package_id, revision),
                "evidence_path": "99_INBOX/READY_FOR_ANALYSIS/cccccccccccccccccccc/evidence-summary.json",
            }
        ],
    }
    _, bad_errors = parse_handoff(bad_handoff, 50)
    assert "item_0_evidence_path" in bad_errors

    index = build_index([envelope], [])
    assert index["active"] == 1
    assert index["items"][0]["path"] == f"{DEFAULT_DESTINATION}/{package_id}.json"
    assert index["candidate_created"] is False

    # Full local-mode smoke verifies persisted envelope/index bytes without rclone.
    with tempfile.TemporaryDirectory(prefix="tl-ready-evidence-test-") as temp_dir:
        root = Path(temp_dir)
        handoff_path = root / "99_INBOX/CURATION/HANDOFF/latest.json"
        evidence_path = root / expected_evidence_path(package_id)
        handoff_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        evidence_path.write_bytes(raw)

        old_argv = sys.argv
        try:
            sys.argv = ["ready_evidence_bridge.py", "--root-dir", str(root)]
            assert main() == 0
        finally:
            sys.argv = old_argv

        persisted = json.loads((root / DEFAULT_DESTINATION / f"{package_id}.json").read_text())
        persisted_index = json.loads((root / DEFAULT_DESTINATION / "index.json").read_text())
        assert persisted["package_id"] == package_id
        assert persisted_index["active"] == 1
        assert persisted_index["held"] == 0

    print("ready_evidence_bridge_smoke_ok active=1 candidate_write=0 canonical_write=0 paid_model=0")


if __name__ == "__main__":
    main_test()
