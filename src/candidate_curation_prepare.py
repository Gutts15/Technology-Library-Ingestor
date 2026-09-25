#!/usr/bin/env python3
"""Prepare private canonical-curation drafts from resolved semantic candidates.

This stage deliberately stops one boundary before publication. It validates the
current candidate bytes, semantic review and proposed route, then writes a
private machine-readable draft package under CANDIDATES/CURATION_DRAFTS.

The draft is NOT a library_publish.py manifest and cannot write to 00_LIBRARY.
This lets the project stabilize candidate->canonical transformation without
risking the existing 25-record curation publish manifest.
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
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_queue import parse_headers, validate_candidate
from candidate_semantic_contract import ALLOWED_EVIDENCE

SCHEMA_VERSION = 1
DRAFT_VERSION = "0.1.0"
DEFAULT_ROOT = "99_INBOX/CANDIDATES"
DEFAULT_READY = "READY_FOR_CURATION"
DEFAULT_REVIEWS = "SEMANTIC_REVIEWS"
DEFAULT_DRAFTS = "CURATION_DRAFTS"
DEFAULT_MASTER = "00_LIBRARY/MASTER_INDEX.md"
MAX_ITEMS = 50

READY_DECISIONS = {"VALIDATED_NEW", "VALIDATED_UPDATE", "SOURCE_ONLY"}
PROMOTION_EVIDENCE = {"HIGH", "MEDIUM"}
TYPE_RE = re.compile(r"^(TECHNOLOGY|PATTERN|PIPELINE|SOURCE)$")
STATUS_RE = re.compile(r"^(REFERENCE|TEST|DEPRECATED)$")
DOMAIN_RE = re.compile(r"^[0-9]{2}_[A-Z0-9_]+$")
CATEGORY_RE = re.compile(r"^[A-Z0-9_]+$")
CANONICAL_PATH_RE = re.compile(r"^00_LIBRARY/[A-Za-z0-9_./-]+\.md$")
MASTER_LINE_RE = re.compile(
    r"^-\s+(TECHNOLOGY|PATTERN|PIPELINE|SOURCE)\s+-\s+(.+?)\s+"
    r"\[(REFERENCE|TEST|DEPRECATED)\]\s+\(`([^`]+)`\)\s*$"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["rclone", *arguments], check=False, capture_output=True)


def remote_bytes(remote: str, relative: str) -> bytes | None:
    result = run_rclone(["cat", join_remote(remote, relative), "--log-level", "ERROR"])
    return result.stdout if result.returncode == 0 else None


def remote_text(remote: str, relative: str) -> str | None:
    raw = remote_bytes(remote, relative)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def remote_json(remote: str, relative: str) -> dict[str, Any] | None:
    text = remote_text(remote, relative)
    if text is None:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    asciiish = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    asciiish = asciiish.casefold()
    asciiish = re.sub(r"[^a-z0-9]+", " ", asciiish)
    return " ".join(asciiish.split())


def slugify(value: str) -> str:
    key = normalize_title(value).replace(" ", "-")
    key = re.sub(r"-+", "-", key).strip("-")
    return key[:120].strip("-")


def source_category_from_domain(domain: str) -> str | None:
    match = re.fullmatch(r"[0-9]{2}_([A-Z0-9_]+)", domain)
    return match.group(1) if match else None


def canonical_target(record_type: str, domain: str, category: str, slug: str) -> str:
    if record_type == "SOURCE":
        return f"00_LIBRARY/SOURCES/source-{slug}.md"
    return f"00_LIBRARY/{domain}/{category}/{record_type.lower()}-{slug}.md"


def master_records(master_text: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for line in master_text.splitlines():
        match = MASTER_LINE_RE.fullmatch(line.strip())
        if not match:
            continue
        record_type, title, status, path = match.groups()
        records.append(
            {
                "record_type": record_type,
                "title": title,
                "title_key": normalize_title(title),
                "status": status,
                "path": path,
            }
        )
    return records


def strip_candidate_headers(text: str) -> str:
    lines = text.splitlines()
    index = 0
    while index < len(lines) and ":" in lines[index] and re.match(r"^[A-Z][A-Z0-9_]{1,63}:\s*", lines[index]):
        index += 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    return "\n".join(lines[index:]).strip()


def review_final(review: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(review, dict) or review.get("schema_version") != 1:
        return None
    value = review.get("final_review")
    return value if isinstance(value, dict) else None


def validate_review_binding(review: dict[str, Any], candidate_id: str, candidate_sha: str) -> list[str]:
    errors: list[str] = []
    if review.get("candidate_id") != candidate_id:
        errors.append("review_candidate_id_mismatch")
    if review.get("candidate_sha256") != candidate_sha:
        errors.append("review_candidate_sha_mismatch")
    if review.get("canonical_write_performed") is not False:
        errors.append("review_canonical_boundary_invalid")
    return errors


def proposed_record(
    candidate_headers: dict[str, str],
    final_review: dict[str, Any],
    master_text: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    decision = final_review.get("decision")
    evidence = final_review.get("evidence_level")
    if decision not in READY_DECISIONS:
        errors.append("decision_not_ready_for_curation")
    if evidence not in ALLOWED_EVIDENCE:
        errors.append("invalid_evidence_level")
    if decision in {"VALIDATED_NEW", "VALIDATED_UPDATE"} and evidence not in PROMOTION_EVIDENCE:
        errors.append("promotion_evidence_too_weak")

    title = candidate_headers.get("TITLE", "").strip()
    proposed_type = final_review.get("proposed_type") or candidate_headers.get("PROPOSED_TYPE")
    proposed_status = final_review.get("proposed_status") or candidate_headers.get("PROPOSED_STATUS")
    candidate_domain = candidate_headers.get("PROPOSED_DOMAIN", "")
    candidate_category = candidate_headers.get("PROPOSED_CATEGORY", "")

    if decision == "SOURCE_ONLY":
        record_type = "SOURCE"
        status = "TEST"
        domain = "SOURCES"
        category = source_category_from_domain(candidate_domain) or candidate_category
    else:
        record_type = proposed_type
        status = proposed_status
        domain = candidate_domain
        category = candidate_category

    if not isinstance(record_type, str) or not TYPE_RE.fullmatch(record_type):
        errors.append("proposed_record_type_invalid")
    if not isinstance(status, str) or not STATUS_RE.fullmatch(status):
        errors.append("proposed_status_invalid")
    if record_type == "SOURCE":
        domain = "SOURCES"
        if not isinstance(category, str) or not CATEGORY_RE.fullmatch(category):
            errors.append("source_category_invalid")
    else:
        if not isinstance(domain, str) or not DOMAIN_RE.fullmatch(domain):
            errors.append("proposed_domain_invalid")
        if not isinstance(category, str) or not CATEGORY_RE.fullmatch(category):
            errors.append("proposed_category_invalid")

    slug = slugify(title)
    if not title or len(title) > 160 or not slug:
        errors.append("title_or_slug_invalid")

    records = master_records(master_text)
    title_matches = [record for record in records if record["title_key"] == normalize_title(title)]
    canonical_match = final_review.get("canonical_match_path")

    if decision == "VALIDATED_NEW" and title_matches:
        errors.append("new_record_title_collision")
    if decision == "VALIDATED_UPDATE":
        if not isinstance(canonical_match, str) or not CANONICAL_PATH_RE.fullmatch(canonical_match):
            errors.append("update_canonical_match_invalid")
        elif canonical_match not in {record["path"] for record in records}:
            errors.append("update_canonical_match_not_current")
    if decision == "SOURCE_ONLY" and title_matches:
        errors.append("source_only_title_collision")

    if errors:
        return None, sorted(set(errors))

    assert isinstance(record_type, str)
    assert isinstance(status, str)
    assert isinstance(domain, str)
    assert isinstance(category, str)
    target = canonical_match if decision == "VALIDATED_UPDATE" else canonical_target(record_type, domain, category, slug)

    return {
        "decision": decision,
        "evidence_level": evidence,
        "record_type": record_type,
        "status": status,
        "domain": domain,
        "category": category,
        "slug": slug,
        "title": title,
        "target_path": target,
        "canonical_match_path": canonical_match if decision == "VALIDATED_UPDATE" else None,
    }, []


def build_draft(
    candidate_text: str,
    review: dict[str, Any] | None,
    master_text: str,
) -> tuple[dict[str, Any] | None, str | None, list[str]]:
    candidate_sha = hashlib.sha256(candidate_text.encode("utf-8")).hexdigest()
    candidate_id = candidate_sha[:20]
    headers, candidate_errors = validate_candidate("candidate-valid.md", candidate_text)
    candidate_errors = [item for item in candidate_errors if item != "invalid_filename"]
    if candidate_errors:
        return None, None, ["candidate_invalid", *candidate_errors]
    if review is None:
        return None, None, ["semantic_review_missing"]
    binding_errors = validate_review_binding(review, candidate_id, candidate_sha)
    final_review = review_final(review)
    if final_review is None:
        return None, None, [*binding_errors, "semantic_final_review_missing"]
    record, record_errors = proposed_record(headers, final_review, master_text)
    errors = sorted(set(binding_errors + record_errors))
    if errors or record is None:
        return None, None, errors

    review_sha = hashlib.sha256(
        json.dumps(review, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    package_id = hashlib.sha256(f"candidate-curation-v1|{candidate_id}".encode()).hexdigest()[:20]
    revision_key = hashlib.sha256(f"{candidate_sha}|{review_sha}".encode()).hexdigest()[:20]
    if record["decision"] == "VALIDATED_UPDATE":
        record_id = None
    else:
        record_id = hashlib.sha256(
            f"candidate-record-v1|{candidate_id}|{record['record_type']}|{record['slug']}".encode()
        ).hexdigest()[:20]

    package = {
        "schema_version": SCHEMA_VERSION,
        "curation_draft_version": DRAFT_VERSION,
        "generated_at": utc_now(),
        "package_id": package_id,
        "revision_key": revision_key,
        "candidate_id": candidate_id,
        "candidate_sha256": candidate_sha,
        "semantic_review_sha256": review_sha,
        "decision": record["decision"],
        "evidence_level": record["evidence_level"],
        "proposed_record": {
            **record,
            "record_id": record_id,
        },
        "publishable": False,
        "canonical_write_performed": False,
        "requires_existing_record_merge": record["decision"] == "VALIDATED_UPDATE",
    }

    material = strip_candidate_headers(candidate_text)
    draft_markdown = (
        "DRAFT_ONLY: TRUE\n"
        f"CANDIDATE_ID: {candidate_id}\n"
        f"DECISION: {record['decision']}\n"
        f"EVIDENCE_LEVEL: {record['evidence_level']}\n"
        f"PROPOSED_TYPE: {record['record_type']}\n"
        f"PROPOSED_STATUS: {record['status']}\n"
        f"PROPOSED_DOMAIN: {record['domain']}\n"
        f"PROPOSED_CATEGORY: {record['category']}\n"
        f"PROPOSED_TARGET: {record['target_path']}\n\n"
        "# Curation Draft Material\n\n"
        "This file is private draft material and is intentionally incompatible with the canonical publisher.\n\n"
        "## Candidate material\n\n"
        f"{material}\n"
    )
    return package, draft_markdown, []


def list_ready(remote: str, root: str, ready: str) -> list[str] | None:
    relative = str(PurePosixPath(root) / ready)
    result = run_rclone([
        "lsjson", join_remote(remote, relative), "--files-only", "--log-level", "ERROR"
    ])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list):
        return None
    names: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("Name") or item.get("Path")
        if isinstance(name, str) and name.startswith("candidate-") and name.endswith(".md"):
            names.append(PurePosixPath(name).name)
    return sorted(set(names))[:MAX_ITEMS]


def upload_pair(remote: str, root: str, drafts: str, candidate_id: str, package: dict[str, Any], markdown: str) -> bool:
    base = str(PurePosixPath(root) / drafts / candidate_id)
    mkdir = run_rclone(["mkdir", join_remote(remote, base), "--log-level", "ERROR"])
    if mkdir.returncode != 0:
        return False
    with tempfile.TemporaryDirectory(prefix="tl-candidate-draft-") as temp_dir:
        temp = Path(temp_dir)
        json_path = temp / "draft.json"
        md_path = temp / "material.md"
        json_path.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        md_path.write_text(markdown, encoding="utf-8")
        for local, name in ((json_path, "draft.json"), (md_path, "material.md")):
            result = run_rclone([
                "copyto", str(local), join_remote(remote, f"{base}/{name}"),
                "--log-level", "ERROR", "--stats", "0",
            ])
            if result.returncode != 0:
                return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare private fail-closed curation drafts from semantic candidates.")
    parser.add_argument("--remote", default="tl:")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--ready", default=DEFAULT_READY)
    parser.add_argument("--reviews", default=DEFAULT_REVIEWS)
    parser.add_argument("--drafts", default=DEFAULT_DRAFTS)
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("candidate_curation_prepare_error code=rclone_missing canonical_write=0")
        return 2
    master_text = remote_text(args.remote, DEFAULT_MASTER)
    if master_text is None:
        print("candidate_curation_prepare_error code=master_index_missing canonical_write=0")
        return 2
    names = list_ready(args.remote, args.root, args.ready)
    if names is None:
        print("candidate_curation_prepare_error code=ready_queue_unavailable canonical_write=0")
        return 2

    prepared = 0
    held = 0
    for name in names:
        candidate_rel = str(PurePosixPath(args.root) / args.ready / name)
        candidate_text = remote_text(args.remote, candidate_rel)
        if candidate_text is None:
            held += 1
            continue
        candidate_sha = hashlib.sha256(candidate_text.encode("utf-8")).hexdigest()
        candidate_id = candidate_sha[:20]
        review_rel = str(PurePosixPath(args.root) / args.reviews / f"{candidate_id}.json")
        review = remote_json(args.remote, review_rel)
        package, markdown, errors = build_draft(candidate_text, review, master_text)
        if errors or package is None or markdown is None:
            held += 1
            continue
        if not upload_pair(args.remote, args.root, args.drafts, candidate_id, package, markdown):
            print("candidate_curation_prepare_error code=draft_write_failed canonical_write=0")
            return 2
        prepared += 1

    print(
        "candidate_curation_prepare_ok "
        f"seen={len(names)} prepared={prepared} held={held} publishable=0 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
