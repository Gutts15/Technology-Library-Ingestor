#!/usr/bin/env python3
"""Deterministic production-boundary checks; no model or network dependency."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_queue import validate_candidate
from file_evidence_candidate_bridge import (
    AUTOMATIC_MODEL_RESPONSE_SCHEMA, FILE_ROOT, Storage, candidate_key,
    model_contract_reason, run, subject_supported,
)
from ready_evidence_bridge import build_envelope, build_index, expected_evidence_path

PACKAGE = "a" * 20
REVISION = "b" * 20
URL = "https://example.com/public-tool"
SHA = "c" * 64


def fixture(
    root: Path,
    *,
    with_url: bool = True,
    kind: str = "link",
    evidence: dict | None = None,
) -> tuple[dict, dict]:
    item = {"package_id": PACKAGE, "revision_key": REVISION, "kind": kind,
            "evidence_path": expected_evidence_path(PACKAGE), "detail_path": None, "preview_path": None}
    summary = {"schema_version": 1, "summary_version": "0.1.0",
               "link": {"url_without_query_or_fragment": URL} if with_url else {},
               "evidence": evidence if evidence is not None else
                           {"samples": ["Public Tool is an open source workflow engine."]}}
    raw = (json.dumps(summary, ensure_ascii=False) + "\n").encode()
    envelope, errors = build_envelope(item, raw)
    assert not errors and envelope is not None
    paths = {expected_evidence_path(PACKAGE): raw,
             f"{FILE_ROOT}/{PACKAGE}.json": json.dumps(envelope).encode(),
             f"{FILE_ROOT}/index.json": json.dumps(build_index([envelope], [])).encode()}
    for relative, content in paths.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return item, envelope


def model_response() -> dict:
    return {"outcome": "CANDIDATES_PROPOSED", "rationale": "Evidence supports one reusable tool.",
            "candidates": [{"title": "Public Tool", "proposed_type": "TECHNOLOGY",
                            "proposed_status": "TEST", "proposed_domain": "04_AI_AGENTS",
                            "proposed_category": "INTEGRATIONS",
                            "summary": "Public Tool is an open source workflow engine.",
                            "claims": ["Public Tool provides a workflow engine."]}]}


def fetched_source(*_args) -> dict:
    return {"status": "OK", "http_status": 200, "excerpt": "Public Tool is an open source workflow engine.",
            "content_sha256": SHA}


def test_create_and_repeat() -> None:
    assert AUTOMATIC_MODEL_RESPONSE_SCHEMA["properties"]["candidates"]["maxItems"] == 1
    automatic_types = AUTOMATIC_MODEL_RESPONSE_SCHEMA["properties"]["candidates"]["items"]["properties"]["proposed_type"]["enum"]
    assert set(automatic_types) == {"TECHNOLOGY", "PATTERN", "PIPELINE"}
    assert model_contract_reason(["candidate_0_fields"]) == "model_contract_candidate_0_fields"
    synthetic_support = (
        "PaletteForge is an AI pixel art generator and editor for game developers. "
        "Generate pixel art sprites from a text prompt and edit sprites with AI."
    )
    assert subject_supported("PaletteForge", synthetic_support)
    assert subject_supported("PaletteForge AI Pixel Art Generator", synthetic_support)
    assert subject_supported("Palette Forge", synthetic_support)
    assert subject_supported("PaletteForge", "https://paletteforge.example.invalid/")
    assert not subject_supported("Invented Platform", synthetic_support)

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture(root)
        storage = Storage(root=root)
        with patch("file_evidence_candidate_bridge.fetch_one", side_effect=fetched_source) as fetch, \
             patch("file_evidence_candidate_bridge.call_local_model", return_value=model_response()) as model:
            assert run(storage, PACKAGE, 8) == ("created", "candidate_created")
            assert fetch.call_count == model.call_count == 1
            assert run(storage, PACKAGE, 8) == ("existing", "already_created")
            assert fetch.call_count == model.call_count == 1
        index = json.loads((root / FILE_ROOT / "index.json").read_text())
        key = candidate_key(index["items"][0])
        candidate = root / "99_INBOX/CANDIDATES/CHAT_RESEARCH" / f"candidate-file-{key}.md"
        content = candidate.read_text(encoding="utf-8")
        _, errors = validate_candidate(candidate.name, content)
        assert not errors, errors
        assert URL in content and f"FILE_EVIDENCE_SHA256: {index['items'][0]['evidence_summary_sha256']}" in content
        assert not (root / "00_LIBRARY").exists()
        receipt = root / FILE_ROOT / "CANDIDATE_RECEIPTS" / f"{key}.json"
        assert json.loads(receipt.read_text())["candidate_sha256"] == hashlib.sha256(candidate.read_bytes()).hexdigest()
        candidate.unlink()  # Simulates a later private lifecycle move.
        with patch("file_evidence_candidate_bridge.fetch_one") as fetch, \
             patch("file_evidence_candidate_bridge.call_local_model") as model:
            assert run(storage, PACKAGE, 8) == ("existing", "already_created")
            fetch.assert_not_called()
            model.assert_not_called()


def test_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture(root, with_url=False, kind="video", evidence={"ocr": [], "speech": []})
        storage = Storage(root=root)
        with patch("file_evidence_candidate_bridge.fetch_one") as fetch, \
             patch("file_evidence_candidate_bridge.call_local_model") as model:
            assert run(storage, PACKAGE, 8, URL) == ("held", "insufficient_file_evidence")
            fetch.assert_not_called()
            model.assert_not_called()
        assert not (root / "99_INBOX/CANDIDATES/CHAT_RESEARCH").exists()

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture(
            root,
            with_url=False,
            kind="video",
            evidence={"ocr": [{"text": "Public Tool"}], "speech": []},
        )
        storage = Storage(root=root)
        with patch("file_evidence_candidate_bridge.fetch_one", side_effect=fetched_source), \
             patch("file_evidence_candidate_bridge.call_local_model", return_value=model_response()) as model:
            assert run(storage, PACKAGE, 8, URL) == ("created", "candidate_created")
            assert model.call_count == 1

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture(root)
        storage = Storage(root=root)
        with patch("file_evidence_candidate_bridge.call_local_model") as model:
            assert run(storage, "d" * 20, 8) == ("held", "package_not_active")
            model.assert_not_called()
        with patch("file_evidence_candidate_bridge.fetch_one", return_value={"status": "ERROR"}), \
             patch("file_evidence_candidate_bridge.call_local_model") as model:
            assert run(storage, PACKAGE, 8) == ("held", "public_source_unverified")
            model.assert_not_called()
        with patch("file_evidence_candidate_bridge.fetch_one", side_effect=fetched_source), \
             patch("file_evidence_candidate_bridge.call_local_model", return_value={"outcome": "NEEDS_REVIEW", "rationale": "Ambiguous", "candidates": []}):
            assert run(storage, PACKAGE, 8) == ("held", "no_reusable_candidate")
        multiple = model_response()
        multiple["candidates"].append({**multiple["candidates"][0], "title": "Other Tool"})
        with patch("file_evidence_candidate_bridge.fetch_one", side_effect=fetched_source), \
             patch("file_evidence_candidate_bridge.call_local_model", return_value=multiple):
            assert run(storage, PACKAGE, 8) == ("held", "ambiguous_or_noncanonical")
        invented = model_response()
        invented["candidates"][0]["title"] = "Invented Platform"
        with patch("file_evidence_candidate_bridge.fetch_one", side_effect=fetched_source), \
             patch("file_evidence_candidate_bridge.call_local_model", return_value=invented):
            assert run(storage, PACKAGE, 8) == ("held", "subject_not_in_evidence")
        assert not (root / "99_INBOX/CANDIDATES/CHAT_RESEARCH").exists()
        raw_path = root / expected_evidence_path(PACKAGE)
        raw_path.write_bytes(raw_path.read_bytes() + b" ")
        with patch("file_evidence_candidate_bridge.fetch_one") as fetch:
            assert run(storage, PACKAGE, 8) == ("held", "evidence_sha_mismatch")
            fetch.assert_not_called()
        raw_path.write_bytes(raw_path.read_bytes()[:-1])
        envelope_path = root / FILE_ROOT / f"{PACKAGE}.json"
        changed = json.loads(envelope_path.read_text())
        changed["semantic_summary"]["evidence"]["samples"] = ["Unsupported replacement text."]
        envelope_path.write_text(json.dumps(changed), encoding="utf-8")
        with patch("file_evidence_candidate_bridge.fetch_one") as fetch:
            assert run(storage, PACKAGE, 8) == ("held", "semantic_summary_mismatch")
            fetch.assert_not_called()


def test_no_source_and_conflict() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture(root, with_url=False)
        with patch("file_evidence_candidate_bridge.call_local_model") as model:
            assert run(Storage(root=root), PACKAGE, 8) == ("held", "public_source_missing")
            model.assert_not_called()

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture(root, with_url=False)
        storage = Storage(root=root)
        with patch("file_evidence_candidate_bridge.fetch_one", side_effect=fetched_source), \
             patch("file_evidence_candidate_bridge.call_local_model", return_value=model_response()):
            assert run(storage, PACKAGE, 8, URL) == ("created", "candidate_created")
        index = json.loads((root / FILE_ROOT / "index.json").read_text())
        key = candidate_key(index["items"][0])
        receipt = json.loads((root / FILE_ROOT / "CANDIDATE_RECEIPTS" / f"{key}.json").read_text())
        assert receipt["source_url_sha256"] == hashlib.sha256(URL.encode("utf-8")).hexdigest()
        assert run(storage, PACKAGE, 8, "https://example.com/other") == ("held", "receipt_invalid")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _, envelope = fixture(root)
        key = candidate_key(envelope)
        path = root / "99_INBOX/CANDIDATES/CHAT_RESEARCH" / f"candidate-file-{key}.md"
        path.parent.mkdir(parents=True)
        path.write_text("unrelated candidate", encoding="utf-8")
        with patch("file_evidence_candidate_bridge.call_local_model") as model:
            assert run(Storage(root=root), PACKAGE, 8) == ("held", "candidate_conflict")
            model.assert_not_called()


if __name__ == "__main__":
    test_create_and_repeat()
    test_fail_closed()
    test_no_source_and_conflict()
    print("file_evidence_candidate_bridge_smoke_ok created=1 idempotent=1 fail_closed=1 canonical_write=0")
