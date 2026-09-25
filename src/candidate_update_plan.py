#!/usr/bin/env python3
"""Build a private, SHA-bound update plan for VALIDATED_UPDATE candidates.

This stage deliberately does not merge or publish canonical bytes. It proves the
identity of the current canonical base record, binds the update to its exact
SHA-256, applies the same deterministic editorial usefulness filter used for new
records, and carries forward only retained claim-level evidence already marked
SUPPORTED.

The output is the safe handoff contract for a future merge renderer. A concurrent
change to the canonical base invalidates the plan before any write can occur.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from candidate_curation_prepare import normalize_title
from candidate_editorial_gate import apply_editorial_gate
from library_index_build import validate_record

SCHEMA_VERSION = 1
UPDATE_PLAN_VERSION = "0.2.0"
ID_RE = re.compile(r"^[0-9a-f]{20}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_STATUS = {"REFERENCE", "TEST", "DEPRECATED"}
MAX_CLAIMS = 20
MAX_PLAN_BYTES = 128 * 1024


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def successful_source_map(probe: dict[str, Any] | None) -> dict[int, dict[str, str]]:
    output: dict[int, dict[str, str]] = {}
    if not isinstance(probe, dict) or probe.get("schema_version") != 1:
        return output
    sources = probe.get("sources")
    if not isinstance(sources, list):
        return output
    for index, raw in enumerate(sources, 1):
        if not isinstance(raw, dict) or raw.get("status") != "OK":
            continue
        url = raw.get("source_url")
        source_sha = raw.get("source_url_sha256")
        if not isinstance(url, str) or not url:
            continue
        if not isinstance(source_sha, str) or not SHA_RE.fullmatch(source_sha):
            source_sha = hashlib.sha256(url.encode("utf-8")).hexdigest()
        output[index] = {"source_url": url, "source_url_sha256": source_sha}
    return output


def build_update_plan(
    package: dict[str, Any] | None,
    claim_review: dict[str, Any] | None,
    probe: dict[str, Any] | None,
    base_content: bytes,
    base_path: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if not isinstance(package, dict) or package.get("schema_version") != 1:
        return None, ["curation_package_invalid"]
    if package.get("decision") != "VALIDATED_UPDATE":
        return None, ["update_decision_required"]
    if package.get("requires_existing_record_merge") is not True:
        errors.append("existing_record_merge_flag_required")
    if package.get("canonical_write_performed") is not False:
        errors.append("curation_package_canonical_write_flag")

    candidate_id = package.get("candidate_id")
    candidate_sha = package.get("candidate_sha256")
    package_id = package.get("package_id")
    revision_key = package.get("revision_key")
    if not isinstance(candidate_id, str) or not ID_RE.fullmatch(candidate_id):
        errors.append("candidate_id")
    if not isinstance(candidate_sha, str) or not SHA_RE.fullmatch(candidate_sha):
        errors.append("candidate_sha256")
    if not isinstance(package_id, str) or not ID_RE.fullmatch(package_id):
        errors.append("package_id")
    if not isinstance(revision_key, str) or not ID_RE.fullmatch(revision_key):
        errors.append("revision_key")

    proposed = package.get("proposed_record")
    if not isinstance(proposed, dict):
        errors.append("proposed_record_missing")
        proposed = {}
    target = proposed.get("target_path")
    canonical_match = proposed.get("canonical_match_path")
    if target != base_path or canonical_match != base_path:
        errors.append("canonical_match_path_mismatch")
    if proposed.get("record_id") is not None:
        errors.append("update_must_not_invent_record_id")

    try:
        current = validate_record(base_path, base_content)
    except ValueError as exc:
        errors.append(f"base_record_{exc}")
        current = None

    if current is not None:
        if proposed.get("record_type") != current["record_type"]:
            errors.append("record_type_identity_mismatch")
        if proposed.get("domain") != current["domain"]:
            errors.append("domain_identity_mismatch")
        if proposed.get("category") != current["category"]:
            errors.append("category_identity_mismatch")
        if normalize_title(str(proposed.get("title") or "")) != normalize_title(current["title"]):
            errors.append("title_identity_mismatch")
    proposed_status = proposed.get("status")
    if proposed_status not in ALLOWED_STATUS:
        errors.append("proposed_status")

    if not isinstance(claim_review, dict) or claim_review.get("schema_version") != 1:
        errors.append("claim_review_invalid")
        publication_claims: list[Any] = []
        editorial = None
    else:
        if claim_review.get("state") != "READY":
            errors.append("claim_review_not_ready")
        if claim_review.get("candidate_id") != candidate_id:
            errors.append("claim_review_candidate_id_mismatch")
        if claim_review.get("candidate_sha256") != candidate_sha:
            errors.append("claim_review_candidate_sha_mismatch")

        editorial, editorial_errors = apply_editorial_gate(
            proposed,
            claim_review,
            require_functional_evidence=False,
        )
        if editorial_errors:
            errors.extend(f"editorial_{value}" for value in editorial_errors)
            publication_claims = []
        elif editorial is None:
            errors.append("editorial_gate_invalid")
            publication_claims = []
        elif editorial.get("state") != "READY":
            reasons = editorial.get("reasons") if isinstance(editorial.get("reasons"), list) else []
            errors.extend(f"editorial_{value}" for value in reasons or ["hold"])
            publication_claims = []
        else:
            publication_claims = editorial.get("publication_claims")
            if not isinstance(publication_claims, list) or not publication_claims:
                errors.append("editorial_claims_missing")
                publication_claims = []
            if len(publication_claims) > MAX_CLAIMS:
                errors.append("supported_claims_too_many")

    sources = successful_source_map(probe)
    if not sources:
        errors.append("successful_sources_missing")

    plan_claims: list[dict[str, Any]] = []
    for position, raw in enumerate(publication_claims):
        if not isinstance(raw, dict) or raw.get("support") != "SUPPORTED":
            errors.append(f"claim_{position}_not_supported")
            continue
        claim = raw.get("claim")
        indices = raw.get("source_indices")
        kind = raw.get("editorial_kind")
        if not isinstance(claim, str) or not claim.strip():
            errors.append(f"claim_{position}_text")
            continue
        if not isinstance(indices, list) or not indices or any(not isinstance(value, int) for value in indices):
            errors.append(f"claim_{position}_sources")
            continue
        if not isinstance(kind, str) or not kind:
            errors.append(f"claim_{position}_editorial_kind")
            continue
        unique = sorted(set(indices))
        if any(index not in sources for index in unique):
            errors.append(f"claim_{position}_source_not_successful")
            continue
        plan_claims.append(
            {
                "claim": claim.strip(),
                "editorial_kind": kind,
                "source_refs": [
                    {
                        "source_index": index,
                        "source_url": sources[index]["source_url"],
                        "source_url_sha256": sources[index]["source_url_sha256"],
                    }
                    for index in unique
                ],
            }
        )

    if errors or current is None:
        return None, sorted(set(errors))

    base_sha = hashlib.sha256(base_content).hexdigest()
    editorial_counts = editorial.get("counts") if isinstance(editorial, dict) else {}
    plan = {
        "schema_version": SCHEMA_VERSION,
        "update_plan_version": UPDATE_PLAN_VERSION,
        "candidate_id": candidate_id,
        "candidate_sha256": candidate_sha,
        "package_id": package_id,
        "revision_key": revision_key,
        "base_record": {
            "record_id": current["record_id"],
            "record_type": current["record_type"],
            "status": current["status"],
            "domain": current["domain"],
            "category": current["category"],
            "title": current["title"],
            "path": base_path,
            "sha256": base_sha,
        },
        "proposed_status": proposed_status,
        "editorial_gate_version": editorial.get("editorial_gate_version") if isinstance(editorial, dict) else None,
        "editorial_dropped_claims": int(editorial_counts.get("dropped") or 0) if isinstance(editorial_counts, dict) else 0,
        "supported_claims": plan_claims,
        "requires_merge_render": True,
        "requires_base_sha_match_before_write": True,
        "canonical_write_performed": False,
    }
    if len(json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_PLAN_BYTES:
        return None, ["update_plan_too_large"]
    return plan, []


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an editorially filtered SHA-bound private plan for one VALIDATED_UPDATE candidate.")
    parser.add_argument("--curation-package", type=Path, required=True)
    parser.add_argument("--claim-review", type=Path, required=True)
    parser.add_argument("--source-probe", type=Path, required=True)
    parser.add_argument("--base-record", type=Path, required=True)
    parser.add_argument("--base-path", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    package = read_json(args.curation_package)
    claim_review = read_json(args.claim_review)
    probe = read_json(args.source_probe)
    try:
        base_content = args.base_record.read_bytes()
    except OSError:
        print("candidate_update_plan_error code=base_record_read_failed canonical_write=0")
        return 2

    plan, errors = build_update_plan(package, claim_review, probe, base_content, args.base_path)
    if errors or plan is None:
        print(f"candidate_update_plan_error codes={','.join(errors or ['plan_failed'])} canonical_write=0")
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_update_plan_ok "
        f"record_id={plan['base_record']['record_id']} claims={len(plan['supported_claims'])} "
        f"editorial_dropped={plan['editorial_dropped_claims']} "
        "requires_merge_render=1 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
