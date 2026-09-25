#!/usr/bin/env python3
"""Dependency-free tests for FILE_EVIDENCE semantic batch behavior."""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import file_evidence_semantic_batch as batch


MASTER = """# MASTER_INDEX

## 04_AI_AGENTS
### INTEGRATIONS
- TECHNOLOGY - Existing Tool [REFERENCE] (`00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-existing-tool.md`)
"""


def envelope(package: str, revision: str, envelope_id: str, evidence_sha: str) -> dict:
    return {
        "schema_version": 1,
        "bridge_version": "0.1.0",
        "type": "FILE_EVIDENCE_ENVELOPE",
        "state": "READY_FOR_SEMANTIC_EXTRACTION",
        "envelope_id": envelope_id,
        "package_id": package,
        "revision_key": revision,
        "kind": "document",
        "evidence_summary_sha256": evidence_sha,
        "pointers": {
            "evidence": f"99_INBOX/READY_FOR_ANALYSIS/{package}/evidence-summary.json",
            "detail": None,
            "preview": None,
        },
        "semantic_summary": {
            "schema_version": 1,
            "content": {"type": "pdf"},
            "evidence": {"samples": ["A reusable technical integration is described here."]},
        },
        "candidate_created": False,
        "curation_transition_performed": False,
        "canonical_write_performed": False,
        "paid_model_used": False,
    }


def index_item(root: str, env: dict) -> dict:
    return {
        "package_id": env["package_id"],
        "revision_key": env["revision_key"],
        "envelope_id": env["envelope_id"],
        "evidence_summary_sha256": env["evidence_summary_sha256"],
        "path": f"{root}/{env['package_id']}.json",
    }


def run_main(index: dict, envelopes: dict[str, dict], model_payload) -> tuple[int, dict[str, dict], str]:
    uploads: dict[str, dict] = {}
    original_argv = sys.argv[:]
    originals = {
        "remote_json": batch.remote_json,
        "remote_text": batch.remote_text,
        "call_ollama": batch.call_ollama,
        "upload_json": batch.upload_json,
        "which": batch.shutil.which,
    }

    def fake_remote_json(_remote: str, relative: str):
        if relative.endswith("/index.json"):
            return index
        return envelopes.get(relative)

    try:
        batch.remote_json = fake_remote_json
        batch.remote_text = lambda _remote, relative: MASTER if relative == batch.DEFAULT_MASTER else None
        batch.call_ollama = lambda *_args, **_kwargs: model_payload
        batch.upload_json = lambda _remote, relative, payload: uploads.setdefault(relative, payload) is payload
        batch.shutil.which = lambda name: "/usr/bin/rclone" if name == "rclone" else None
        sys.argv = [
            "file_evidence_semantic_batch.py",
            "--model",
            "synthetic-local-model",
            "--root",
            batch.DEFAULT_ROOT,
        ]
        stream = io.StringIO()
        with redirect_stdout(stream):
            rc = batch.main()
        return rc, uploads, stream.getvalue()
    finally:
        batch.remote_json = originals["remote_json"]
        batch.remote_text = originals["remote_text"]
        batch.call_ollama = originals["call_ollama"]
        batch.upload_json = originals["upload_json"]
        batch.shutil.which = originals["which"]
        sys.argv = original_argv


def main() -> None:
    root = batch.DEFAULT_ROOT
    good = envelope("1" * 20, "2" * 20, "3" * 20, "4" * 64)
    stale = envelope("5" * 20, "6" * 20, "7" * 20, "8" * 64)
    stale_on_drive = dict(stale)
    stale_on_drive["revision_key"] = "9" * 20

    index = {
        "schema_version": 1,
        "type": "FILE_EVIDENCE_INDEX",
        "items": [index_item(root, good), index_item(root, stale)],
    }
    items, errors = batch.active_items(index, 20, root)
    assert not errors, errors
    assert len(items) == 2
    assert batch.indexed_binding_matches(items[0], good) is True
    assert batch.indexed_binding_matches(items[1], stale_on_drive) is False

    envs = {
        f"{root}/{good['package_id']}.json": good,
        f"{root}/{stale['package_id']}.json": stale_on_drive,
    }
    rc, uploads, stdout = run_main(index, envs, None)
    assert rc == 0, stdout
    plan_path = f"{root}/{batch.DEFAULT_PLANS}/{good['package_id']}.json"
    state_path = f"{root}/{batch.DEFAULT_STATE}"
    assert uploads[plan_path]["outcome"] == "NEEDS_REVIEW"
    assert uploads[plan_path]["candidate_write_performed"] is False
    assert uploads[plan_path]["curation_transition_performed"] is False
    assert uploads[plan_path]["canonical_write_performed"] is False
    assert uploads[plan_path]["paid_model_used"] is False
    assert f"{root}/{batch.DEFAULT_PLANS}/{stale['package_id']}.json" not in uploads
    state = uploads[state_path]
    assert state["processed"] == 1
    assert state["held"] == 1
    assert state["fail_closed_items"] == 1
    assert state["held_items"] == [
        {"package_id": stale["package_id"], "reason": "indexed_envelope_binding_mismatch"}
    ]
    assert state["outcome_counts"] == {"NEEDS_REVIEW": 1}
    assert state["candidate_write_performed"] is False
    assert state["curation_transition_performed"] is False
    assert state["canonical_write_performed"] is False
    assert state["paid_model_used"] is False
    assert "processed=1 held=1 fail_closed=1" in stdout

    # A structurally invalid model answer must also collapse to a valid
    # NEEDS_REVIEW plan rather than escaping the batch contract.
    single_index = {
        "schema_version": 1,
        "type": "FILE_EVIDENCE_INDEX",
        "items": [index_item(root, good)],
    }
    single_envs = {f"{root}/{good['package_id']}.json": good}
    rc2, uploads2, stdout2 = run_main(single_index, single_envs, {"unexpected": "shape"})
    assert rc2 == 0, stdout2
    assert uploads2[plan_path]["outcome"] == "NEEDS_REVIEW"
    assert uploads2[state_path]["processed"] == 1
    assert uploads2[state_path]["held"] == 0
    assert uploads2[state_path]["fail_closed_items"] == 1

    unsafe_index = {
        "schema_version": 1,
        "type": "FILE_EVIDENCE_INDEX",
        "items": [{**index_item(root, good), "path": "99_INBOX/CANDIDATES/WRONG/path.json"}],
    }
    unsafe_items, unsafe_errors = batch.active_items(unsafe_index, 20, root)
    assert not unsafe_items
    assert "item_0_path" in unsafe_errors

    snapshot = batch.build_batch_state([], 0, [])
    assert snapshot["candidate_write_performed"] is False
    assert snapshot["canonical_write_performed"] is False
    assert snapshot["paid_model_used"] is False

    print("file_evidence_semantic_batch_smoke_ok binding_guard=1 model_failure_fail_closed=1 invalid_model_fail_closed=1 candidate_write=0 canonical_write=0 paid_model=0")


if __name__ == "__main__":
    main()
