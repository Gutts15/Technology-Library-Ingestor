#!/usr/bin/env python3
"""Prepare an isolated publisher-compatible candidate outbox without publishing it.

The established canonical curation manifest under 99_INBOX/CURATION/PUBLISH_READY
is intentionally left untouched. This stage renders only claim-level supported
material that also survives the deterministic canonical editorial gate, using the
same type-aware body templates as the non-publishable canonical preview.

Current library_publish.py deliberately rejects this candidate outbox because its
content_path prefix is different. This file never writes 00_LIBRARY.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_canonical_draft import successful_source_urls
from candidate_editorial_gate import apply_editorial_gate, claims_by_kind
from candidate_record_template import TEMPLATE_VERSION, render_body

SCHEMA_VERSION = 1
PREPARE_VERSION = "0.3.0"
PUBLISH_SCHEMA_VERSION = "0.2.0"
DEFAULT_ROOT = "99_INBOX/CANDIDATES"
DEFAULT_OUTBOX = "PUBLISH_READY"
MAX_CONTENT_BYTES = 128 * 1024
ID_RE = re.compile(r"^[0-9a-f]{20}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
TYPE_VALUES = {"TECHNOLOGY", "PATTERN", "PIPELINE", "SOURCE"}
STATUS_VALUES = {"REFERENCE", "TEST", "DEPRECATED"}
DOMAIN_RE = re.compile(r"^(?:[0-9]{2}_[A-Z0-9_]+|SOURCES)$")
CATEGORY_RE = re.compile(r"^[A-Z0-9_]+$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def validation_date(probe: dict[str, Any] | None) -> str:
    if isinstance(probe, dict):
        raw = probe.get("generated_at")
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
            except ValueError:
                pass
    return datetime.now(timezone.utc).date().isoformat()


def validate_record(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    record_id = record.get("record_id")
    record_type = record.get("record_type")
    status = record.get("status")
    domain = record.get("domain")
    category = record.get("category")
    slug = record.get("slug")
    title = record.get("title")
    if not isinstance(record_id, str) or not ID_RE.fullmatch(record_id):
        errors.append("record_id_invalid")
    if record_type not in TYPE_VALUES:
        errors.append("record_type_invalid")
    if status not in STATUS_VALUES:
        errors.append("record_status_invalid")
    if not isinstance(domain, str) or not DOMAIN_RE.fullmatch(domain):
        errors.append("record_domain_invalid")
    if not isinstance(category, str) or not CATEGORY_RE.fullmatch(category):
        errors.append("record_category_invalid")
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        errors.append("record_slug_invalid")
    if not isinstance(title, str) or not title.strip() or len(title) > 160:
        errors.append("record_title_invalid")
    if record_type == "SOURCE" and domain != "SOURCES":
        errors.append("source_domain_invalid")
    if record_type != "SOURCE" and domain == "SOURCES":
        errors.append("canonical_domain_invalid")
    return sorted(set(errors))


def _header_summary(items: list[dict[str, Any]], fallback: str) -> str:
    values = [str(item.get("claim") or "").strip() for item in items]
    values = [value for value in values if value]
    return "; ".join(values[:2]) if values else fallback


def build_content(
    curation: dict[str, Any] | None,
    claim_review: dict[str, Any] | None,
    probe: dict[str, Any] | None,
) -> tuple[bytes | None, dict[str, Any] | None, list[str]]:
    if not isinstance(curation, dict) or curation.get("schema_version") != 1:
        return None, None, ["curation_draft_invalid"]
    if curation.get("publishable") is not False or curation.get("canonical_write_performed") is not False:
        return None, None, ["curation_boundary_invalid"]
    decision = curation.get("decision")
    if decision not in {"VALIDATED_NEW", "SOURCE_ONLY"}:
        return None, None, ["decision_not_supported_for_new_record_prepare"]

    if not isinstance(claim_review, dict) or claim_review.get("state") != "READY":
        return None, None, ["claim_review_not_ready"]
    if curation.get("candidate_id") != claim_review.get("candidate_id"):
        return None, None, ["candidate_id_mismatch"]
    if curation.get("candidate_sha256") != claim_review.get("candidate_sha256"):
        return None, None, ["candidate_sha_mismatch"]

    record = curation.get("proposed_record")
    if not isinstance(record, dict):
        return None, None, ["proposed_record_missing"]
    errors = validate_record(record)
    if errors:
        return None, None, errors

    editorial, editorial_errors = apply_editorial_gate(record, claim_review)
    if editorial_errors or editorial is None:
        return None, None, editorial_errors
    if editorial.get("state") != "READY":
        reasons = editorial.get("reasons") if isinstance(editorial.get("reasons"), list) else []
        return None, None, [f"editorial_{reason}" for reason in reasons] or ["editorial_gate_hold"]

    claims = editorial["publication_claims"]
    urls = successful_source_urls(probe)
    used_refs = sorted({ref for item in claims for ref in item["source_indices"]})
    if any(ref not in urls for ref in used_refs):
        return None, None, ["supported_claim_source_missing"]

    record_id = str(record["record_id"])
    record_type = str(record["record_type"])
    status = str(record["status"])
    domain = str(record["domain"])
    category = str(record["category"])
    title = str(record["title"])
    slug = str(record["slug"])
    checked = validation_date(probe)

    license_claims = claims_by_kind(editorial, "LICENSE")
    compatibility_claims = claims_by_kind(editorial, "COMPATIBILITY")
    license_header = _header_summary(license_claims, "Not established by validated evidence.")
    compatibility_header = _header_summary(
        compatibility_claims, "Not established by validated evidence."
    )

    try:
        body = render_body(record_type, title, editorial)
    except ValueError as exc:
        return None, None, [str(exc)]

    lines = [
        f"RECORD_ID: {record_id}",
        f"TYPE: {record_type}",
        f"STATUS: {status}",
        f"DOMAIN: {domain}",
        f"CATEGORY: {category}",
        f"TITLE: {title}",
        "COST: Not established by validated evidence.",
        f"LICENSE: {license_header}",
        f"COMPATIBILITY: {compatibility_header}",
        f"LAST_CHECKED: {checked}",
        f"SOURCE: validated explicit-source evidence from chat-research candidate {curation['candidate_id']}; see REFERENCES",
        "",
        *body,
        "",
        "## REFERENCES",
        "",
    ]
    for ref in used_refs:
        lines.append(f"- S{ref}: {urls[ref]}")

    counts = claim_review.get("counts") if isinstance(claim_review.get("counts"), dict) else {}
    claim_excluded = int(counts.get("partial") or 0) + int(counts.get("unsupported") or 0)
    editorial_counts = editorial.get("counts") if isinstance(editorial.get("counts"), dict) else {}
    lines.extend(
        [
            "",
            "## VALIDATION NOTES",
            "",
            f"Evidence level: {curation.get('evidence_level')}.",
            f"Claims excluded because they were not fully supported: {claim_excluded}.",
            f"Supported claims excluded as volatile or low-value canonical material: {int(editorial_counts.get('dropped') or 0)}.",
            f"Editorial gate version: {editorial.get('editorial_gate_version')}.",
            f"Type-aware candidate template: {record_type} / {TEMPLATE_VERSION}.",
            "Only claim-level SUPPORTED evidence retained by the canonical editorial gate is included above.",
            "",
        ]
    )

    content = "\n".join(lines).encode("utf-8")
    if not content or len(content) > MAX_CONTENT_BYTES:
        return None, None, ["content_size_invalid"]
    content_sha = hashlib.sha256(content).hexdigest()
    outbox_path = f"99_INBOX/CANDIDATES/{DEFAULT_OUTBOX}/records/{record_id}.md"
    manifest_item = {
        "package_id": curation.get("package_id"),
        "revision_key": curation.get("revision_key"),
        "record_id": record_id,
        "record_type": record_type,
        "status": status,
        "domain": domain,
        "category": category,
        "slug": slug,
        "title": title,
        "content_path": outbox_path,
        "content_sha256": content_sha,
    }
    if not isinstance(manifest_item["package_id"], str) or not ID_RE.fullmatch(manifest_item["package_id"]):
        return None, None, ["package_id_invalid"]
    if not isinstance(manifest_item["revision_key"], str) or not ID_RE.fullmatch(manifest_item["revision_key"]):
        return None, None, ["revision_key_invalid"]
    return content, manifest_item, []


def build_manifest(items: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[str]]:
    seen_ids: set[str] = set()
    seen_targets: set[tuple[str, str, str, str]] = set()
    errors: list[str] = []
    for index, item in enumerate(items):
        record_id = item.get("record_id")
        target_key = (
            str(item.get("record_type")),
            str(item.get("domain")),
            str(item.get("category")),
            str(item.get("slug")),
        )
        if not isinstance(record_id, str) or not ID_RE.fullmatch(record_id):
            errors.append(f"item_{index}_record_id")
            continue
        if record_id in seen_ids:
            errors.append(f"item_{index}_duplicate_record_id")
        if target_key in seen_targets:
            errors.append(f"item_{index}_duplicate_target")
        seen_ids.add(record_id)
        seen_targets.add(target_key)
    if errors:
        return None, sorted(set(errors))
    return {
        "schema_version": SCHEMA_VERSION,
        "publish_version": PUBLISH_SCHEMA_VERSION,
        "items": sorted(items, key=lambda item: (str(item["package_id"]), str(item["record_id"]))),
    }, []


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["rclone", *arguments], check=False, capture_output=True)


def write_outbox(
    remote: str,
    root: str,
    candidate_id: str,
    content: bytes,
    manifest: dict[str, Any],
) -> bool:
    item = manifest["items"][0]
    record_id = item["record_id"]
    base = str(PurePosixPath(root) / DEFAULT_OUTBOX)
    records = str(PurePosixPath(base) / "records")
    if run_rclone(["mkdir", join_remote(remote, records), "--log-level", "ERROR"]).returncode != 0:
        return False
    with tempfile.TemporaryDirectory(prefix="tl-candidate-publish-") as temp_dir:
        temp = Path(temp_dir)
        content_path = temp / f"{record_id}.md"
        manifest_path = temp / f"manifest-{candidate_id}.json"
        content_path.write_bytes(content)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        copies = [
            (content_path, str(PurePosixPath(records) / f"{record_id}.md")),
            (manifest_path, str(PurePosixPath(base) / f"candidate-{candidate_id}.json")),
        ]
        for local, relative in copies:
            if run_rclone([
                "copyto", str(local), join_remote(remote, relative),
                "--log-level", "ERROR", "--stats", "0",
            ]).returncode != 0:
                return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare editorially filtered isolated candidate publisher outbox.")
    parser.add_argument("--curation-draft", type=Path, required=True)
    parser.add_argument("--claim-review", type=Path, required=True)
    parser.add_argument("--source-probe", type=Path, required=True)
    parser.add_argument("--manifest-out", type=Path)
    parser.add_argument("--content-out", type=Path)
    parser.add_argument("--remote")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    args = parser.parse_args()

    curation = read_json(args.curation_draft)
    claims = read_json(args.claim_review)
    probe = read_json(args.source_probe)
    content, item, errors = build_content(curation, claims, probe)
    if errors or content is None or item is None:
        print(f"candidate_publish_prepare_error codes={','.join(sorted(set(errors or ['prepare_failed'])))} canonical_write=0")
        return 2
    manifest, manifest_errors = build_manifest([item])
    if manifest_errors or manifest is None:
        print(f"candidate_publish_prepare_error codes={','.join(sorted(set(manifest_errors)))} canonical_write=0")
        return 2

    if args.content_out:
        args.content_out.parent.mkdir(parents=True, exist_ok=True)
        args.content_out.write_bytes(content)
    if args.manifest_out:
        args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
        args.manifest_out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.remote:
        if not shutil.which("rclone"):
            print("candidate_publish_prepare_error codes=rclone_missing canonical_write=0")
            return 2
        candidate_id = str(curation.get("candidate_id")) if isinstance(curation, dict) else "unknown"
        if not write_outbox(args.remote, args.root, candidate_id, content, manifest):
            print("candidate_publish_prepare_error codes=outbox_write_failed canonical_write=0")
            return 2

    print(
        "candidate_publish_prepare_ok "
        f"items=1 record_id={item['record_id']} template={TEMPLATE_VERSION} "
        "isolated_outbox=1 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
