#!/usr/bin/env python3
"""Render a non-publishable canonical draft from supported, useful evidence only.

The renderer intentionally does not copy the whole candidate body. It consumes a
curation draft package plus a claim-level evidence review, applies the deterministic
canonical editorial gate, and emits a type-aware Markdown preview containing only
supported claims that are stable/useful enough for canonical knowledge.

The result uses PROPOSED_* headers rather than publisher headers, so it cannot
accidentally satisfy library_publish.py's canonical content contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from candidate_editorial_gate import apply_editorial_gate
from candidate_record_template import TEMPLATE_VERSION, render_body

SCHEMA_VERSION = 1
RENDER_VERSION = "0.3.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def successful_source_urls(probe: dict[str, Any] | None) -> dict[int, str]:
    output: dict[int, str] = {}
    if not isinstance(probe, dict) or not isinstance(probe.get("sources"), list):
        return output
    for index, item in enumerate(probe["sources"], 1):
        if not isinstance(item, dict) or item.get("status") != "OK":
            continue
        url = item.get("source_url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            output[index] = url
    return output


def validate_bindings(
    curation: dict[str, Any] | None,
    claims: dict[str, Any] | None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(curation, dict) or curation.get("schema_version") != 1:
        return ["curation_draft_invalid"]
    if curation.get("publishable") is not False or curation.get("canonical_write_performed") is not False:
        errors.append("curation_boundary_invalid")
    if not isinstance(claims, dict) or claims.get("schema_version") != 1:
        return [*errors, "claim_review_invalid"]
    if claims.get("canonical_write_performed") is not False or claims.get("paid_model_used") is not False:
        errors.append("claim_review_boundary_invalid")
    if curation.get("candidate_id") != claims.get("candidate_id"):
        errors.append("candidate_id_mismatch")
    if curation.get("candidate_sha256") != claims.get("candidate_sha256"):
        errors.append("candidate_sha_mismatch")
    if claims.get("state") != "READY":
        errors.append("claim_review_not_ready")
    return sorted(set(errors))


def build_markdown(
    curation: dict[str, Any] | None,
    claims: dict[str, Any] | None,
    probe: dict[str, Any] | None,
) -> tuple[str | None, dict[str, Any] | None, list[str]]:
    errors = validate_bindings(curation, claims)
    if errors or curation is None or claims is None:
        return None, None, errors
    record = curation.get("proposed_record")
    if not isinstance(record, dict):
        return None, None, ["proposed_record_missing"]
    if record.get("decision") == "VALIDATED_UPDATE":
        return None, None, ["update_requires_existing_record_merge"]

    editorial, editorial_errors = apply_editorial_gate(record, claims)
    if editorial_errors or editorial is None:
        return None, None, editorial_errors
    if editorial.get("state") != "READY":
        reasons = editorial.get("reasons") if isinstance(editorial.get("reasons"), list) else []
        return None, None, [f"editorial_{reason}" for reason in reasons] or ["editorial_gate_hold"]

    supported = editorial["publication_claims"]
    source_urls = successful_source_urls(probe)
    for index, item in enumerate(supported):
        missing = [ref for ref in item["source_indices"] if ref not in source_urls]
        if missing:
            return None, None, [f"claim_{index}_source_not_successful"]

    record_id = record.get("record_id")
    record_type = record.get("record_type")
    status = record.get("status")
    domain = record.get("domain")
    category = record.get("category")
    title = record.get("title")
    target = record.get("target_path")
    required = (record_id, record_type, status, domain, category, title, target)
    if not all(isinstance(value, str) and value for value in required):
        return None, None, ["proposed_record_incomplete"]

    try:
        body = render_body(str(record_type), str(title), editorial)
    except ValueError as exc:
        return None, None, [str(exc)]

    lines = [
        "DRAFT_ONLY: TRUE",
        f"PROPOSED_RECORD_ID: {record_id}",
        f"PROPOSED_TYPE: {record_type}",
        f"PROPOSED_STATUS: {status}",
        f"PROPOSED_DOMAIN: {domain}",
        f"PROPOSED_CATEGORY: {category}",
        f"PROPOSED_TITLE: {title}",
        f"PROPOSED_TARGET: {target}",
        f"CANDIDATE_ID: {curation['candidate_id']}",
        f"EVIDENCE_LEVEL: {curation['evidence_level']}",
        f"EDITORIAL_GATE_VERSION: {editorial['editorial_gate_version']}",
        f"TEMPLATE_VERSION: {TEMPLATE_VERSION}",
        "",
        *body,
    ]

    lines.extend(["", "## VERIFIED REFERENCES", ""])
    used_refs = sorted({ref for item in supported for ref in item["source_indices"]})
    for ref in used_refs:
        lines.append(f"- S{ref}: {source_urls[ref]}")

    excluded_counts = claims.get("counts") if isinstance(claims.get("counts"), dict) else {}
    partial = int(excluded_counts.get("partial") or 0)
    unsupported = int(excluded_counts.get("unsupported") or 0)
    editorial_counts = editorial.get("counts") if isinstance(editorial.get("counts"), dict) else {}
    lines.extend(
        [
            "",
            "## CURATION NOTES",
            "",
            "This draft contains only claims classified as SUPPORTED by the claim-level evidence stage and retained by the canonical editorial gate.",
            f"Partial claims excluded: {partial}.",
            f"Unsupported claims excluded: {unsupported}.",
            f"Supported but low-value/volatile claims excluded by editorial policy: {int(editorial_counts.get('dropped') or 0)}.",
            f"Type-aware candidate template: {record_type} / {TEMPLATE_VERSION}.",
            "It is still not a canonical record and cannot be passed directly to the publisher.",
            "",
        ]
    )
    markdown = "\n".join(lines)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "render_version": RENDER_VERSION,
        "template_version": TEMPLATE_VERSION,
        "generated_at": utc_now(),
        "candidate_id": curation["candidate_id"],
        "candidate_sha256": curation["candidate_sha256"],
        "curation_revision_key": curation["revision_key"],
        "record_id": record_id,
        "record_type": record_type,
        "target_path": target,
        "supported_claims": int(editorial_counts.get("kept") or 0),
        "editorial_dropped_claims": int(editorial_counts.get("dropped") or 0),
        "source_references": len(used_refs),
        "editorial_gate_version": editorial["editorial_gate_version"],
        "markdown_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        "publishable": False,
        "canonical_write_performed": False,
    }
    return markdown, metadata, []


def main() -> int:
    parser = argparse.ArgumentParser(description="Render an editorially filtered non-publishable canonical draft.")
    parser.add_argument("--curation-draft", type=Path, required=True)
    parser.add_argument("--claim-review", type=Path, required=True)
    parser.add_argument("--source-probe", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    parser.add_argument("--metadata-out", type=Path, required=True)
    args = parser.parse_args()

    curation = read_json(args.curation_draft)
    claims = read_json(args.claim_review)
    probe = read_json(args.source_probe)
    markdown, metadata, errors = build_markdown(curation, claims, probe)
    if errors or markdown is None or metadata is None:
        print(f"candidate_canonical_draft_error codes={','.join(sorted(set(errors or ['render_failed'])))} canonical_write=0")
        return 2

    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.metadata_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.write_text(markdown, encoding="utf-8")
    args.metadata_out.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_canonical_draft_ok "
        f"supported={metadata['supported_claims']} dropped={metadata['editorial_dropped_claims']} "
        f"refs={metadata['source_references']} template={metadata['template_version']} "
        "publishable=0 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
