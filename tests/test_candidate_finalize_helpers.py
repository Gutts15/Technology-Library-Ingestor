#!/usr/bin/env python3
"""Smoke tests for local finalizer/cycle helpers without rclone/Ollama side effects."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_finalize_local import choose_final_probe, probe_success_count, semantic_probe
from candidate_local_cycle import (
    decision_ids,
    expected_selected_ids,
    forced_ready_ids,
    selected_from_state,
    stage_commands,
)


def main() -> None:
    valid = {
        "schema_version": 1,
        "source_probe": {
            "schema_version": 1,
            "source_count": 1,
            "successful_sources": 1,
            "sources": [
                {
                    "status": "OK",
                    "source_url": "https://example.com/official",
                    "excerpt": "supported evidence",
                }
            ],
        },
    }
    assert semantic_probe(valid) == valid["source_probe"]
    assert semantic_probe({"schema_version": 1}) is None
    assert semantic_probe({"source_probe": {"schema_version": 2}}) is None
    assert semantic_probe(None) is None

    stored_probe = valid["source_probe"]
    fresh_probe = {
        "schema_version": 1,
        "successful_sources": 1,
        "sources": [
            {
                "status": "OK",
                "source_url": "https://github.com/example/repo",
                "excerpt": "fresh README evidence",
                "evidence_kind": "github_readme",
            }
        ],
    }
    failed_probe = {
        "schema_version": 1,
        "successful_sources": 0,
        "sources": [{"status": "ERROR", "reason": "network_error"}],
    }
    assert probe_success_count(stored_probe) == 1
    assert probe_success_count({"schema_version": 1, "sources": [{"status": "OK"}]}) == 1
    assert probe_success_count(None) == 0
    selected_probe, selected_mode = choose_final_probe(stored_probe, fresh_probe)
    assert selected_probe == fresh_probe and selected_mode == "REFRESHED"
    fallback_probe, fallback_mode = choose_final_probe(stored_probe, failed_probe)
    assert fallback_probe == stored_probe and fallback_mode == "STORED_FALLBACK"
    missing_probe, missing_mode = choose_final_probe(None, failed_probe)
    assert missing_probe is None and missing_mode == "UNAVAILABLE"

    assert selected_from_state({"schema_version": 1, "selected": 2}) == 2
    assert selected_from_state({"schema_version": 1, "selected": 0}) == 0
    assert selected_from_state({"schema_version": 1, "selected": True}) is None
    assert selected_from_state({"schema_version": 2, "selected": 2}) is None
    assert selected_from_state(None) is None

    proposals = {
        "schema_version": 1,
        "entries": [
            {"candidate_id": "a" * 20, "proposal": "READY_FOR_SEMANTIC", "captured_at": "2026-09-01T00:00:00+00:00"},
            {"candidate_id": "b" * 20, "proposal": "READY_FOR_SEMANTIC", "captured_at": "2026-09-01T00:00:00+00:00"},
            {"candidate_id": "c" * 20, "proposal": "NEEDS_REVIEW", "captured_at": "2026-09-01T00:00:00+00:00"},
        ],
    }
    index = {
        "schema_version": 1,
        "entries": [
            {"candidate_id": "a" * 20, "sha256": "1" * 64, "valid": True, "path": "CHAT_RESEARCH/candidate-a.md", "last_checked": "2026-09-01"},
            {"candidate_id": "b" * 20, "sha256": "2" * 64, "valid": True, "path": "CHAT_RESEARCH/candidate-b.md", "last_checked": "2026-09-01"},
        ],
    }
    assert forced_ready_ids(proposals, 1) == ["a" * 20]
    forced, forced_error = expected_selected_ids(
        index,
        proposals,
        force_semantic=True,
        volume_threshold=5,
        max_wait_days=30,
        max_batch=20,
    )
    assert forced_error is None
    assert forced == ["a" * 20, "b" * 20]

    # With the normal threshold and only two candidates, the wrapper knows no
    # semantic batch is due before launching the semantic stage. It therefore
    # cannot accidentally consume stale decisions from a previous run.
    adaptive, adaptive_error = expected_selected_ids(
        index,
        proposals,
        force_semantic=False,
        volume_threshold=5,
        max_wait_days=3650,
        max_batch=20,
    )
    assert adaptive_error is None
    assert adaptive == []

    assert decision_ids({"schema_version": 1, "items": [{"candidate_id": "a" * 20}]}) == ["a" * 20]
    assert decision_ids({"schema_version": 1, "items": [{"candidate_id": 1}]}) is None
    assert decision_ids(None) is None

    queue, validator, semantic, finalize = stage_commands(
        python_exe="python3",
        remote="tl:",
        root="99_INBOX/CANDIDATES",
        model="local-test-model",
        endpoint="http://127.0.0.1:11434",
        timeout=120.0,
        force_semantic=True,
    )
    assert queue[:2] == ["python3", "src/candidate_queue.py"]
    assert validator[:2] == ["python3", "src/candidate_validator.py"]
    assert semantic[:2] == ["python3", "src/candidate_semantic_batch.py"]
    assert semantic[-1] == "--force"
    assert "--volume-threshold" in semantic
    assert "--max-wait-days" in semantic
    assert "--max-batch" in semantic
    assert finalize[:2] == ["python3", "src/candidate_finalize_local.py"]
    assert "--max-items" in finalize
    assert "--force" not in finalize

    _, _, adaptive_semantic, _ = stage_commands(
        python_exe="python3",
        remote="tl:",
        root="99_INBOX/CANDIDATES",
        model="local-test-model",
        endpoint="http://127.0.0.1:11434",
        timeout=120.0,
        force_semantic=False,
    )
    assert "--force" not in adaptive_semantic

    print(
        "candidate_finalize_cycle_helpers_ok stale_state_safe=1 fresh_source_preferred=1 "
        "stored_source_fallback=1 canonical_write=0 paid_model=0"
    )


if __name__ == "__main__":
    main()
