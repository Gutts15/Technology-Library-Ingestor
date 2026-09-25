#!/usr/bin/env python3
"""Render a SHA-bound VALIDATED_UPDATE plan into merged canonical Markdown.

This stage is intentionally non-publishing. It consumes an update plan that has
already passed semantic, claim-level and editorial gates, rechecks the exact base
SHA/identity, preserves existing canonical material, and merges retained supported
claims into stable evidence sections.

It never writes to 00_LIBRARY. The output is local/private merged bytes plus a
metadata report suitable for a future transactional publisher.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from library_index_build import validate_record

SCHEMA_VERSION = 1
MERGE_RENDER_VERSION = "0.1.0"
UPDATE_PLAN_VERSION = "0.1.0"
MAX_OUTPUT_BYTES = 128 * 1024
ALLOWED_STATUS = {"REFERENCE", "TEST", "DEPRECATED"}
ALLOWED_KINDS = {
    "CAPABILITY",
    "COMPATIBILITY",
    "INSTALLATION",
    "LICENSE",
    "LIMITATION",
    "IDENTITY",
    "OTHER",
}
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[0-9a-f]{20}$")
H2_RE = re.compile(r"^##\s+(.+?)\s*$")

SECTION_TARGET = {
    "CAPABILITY": "VERIFIED CAPABILITIES",
    "COMPATIBILITY": "COMPATIBILITY AND SETUP",
    "INSTALLATION": "COMPATIBILITY AND SETUP",
    "LIMITATION": "LIMITATIONS AND SAFETY",
    "LICENSE": "LICENSE",
    "IDENTITY": "OTHER VERIFIED TECHNICAL NOTES",
    "OTHER": "OTHER VERIFIED TECHNICAL NOTES",
}

SECTION_ALIASES = {
    "VERIFIED CAPABILITIES": {
        "VERIFIED CAPABILITIES",
        "CAPABILITIES",
        "VERIFIED CLAIMS",
    },
    "COMPATIBILITY AND SETUP": {
        "COMPATIBILITY AND SETUP",
        "COMPATIBILITY",
        "INSTALLATION",
        "SETUP",
    },
    "LIMITATIONS AND SAFETY": {
        "LIMITATIONS AND SAFETY",
        "LIMITATIONS",
        "DO NOT ASSUME",
        "SAFETY",
    },
    "LICENSE": {"LICENSE", "LICENSING"},
    "OTHER VERIFIED TECHNICAL NOTES": {
        "OTHER VERIFIED TECHNICAL NOTES",
        "VERIFIED TECHNICAL NOTES",
        "OTHER VERIFIED NOTES",
    },
}

REFERENCE_HEADINGS = {
    "REFERENCES",
    "VERIFIED REFERENCES",
    "SOURCES",
    "SOURCE",
    "CURATION NOTES",
}


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def normalize_heading(value: str) -> str:
    return " ".join(value.upper().split())


def bullet_claim(line: str) -> str | None:
    stripped = line.strip()
    if not stripped.startswith("- "):
        return None
    value = stripped[2:].strip()
    return value or None


def existing_claim_keys(lines: list[str]) -> set[str]:
    keys: set[str] = set()
    for line in lines:
        claim = bullet_claim(line)
        if claim:
            keys.add(normalize_text(claim))
    return keys


def find_section(lines: list[str], canonical_heading: str) -> tuple[int, int] | None:
    aliases = SECTION_ALIASES[canonical_heading]
    start: int | None = None
    for index, line in enumerate(lines):
        match = H2_RE.match(line)
        if not match:
            continue
        heading = normalize_heading(match.group(1))
        if start is None:
            if heading in aliases:
                start = index
            continue
        # First H2 after the selected section closes it.
        return start, index
    return (start, len(lines)) if start is not None else None


def reference_insert_index(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        match = H2_RE.match(line)
        if match and normalize_heading(match.group(1)) in REFERENCE_HEADINGS:
            return index
    return len(lines)


def append_to_section(lines: list[str], heading: str, claims: list[str]) -> list[str]:
    if not claims:
        return lines
    bounds = find_section(lines, heading)
    bullets = [f"- {claim}" for claim in claims]
    if bounds is not None:
        _, end = bounds
        insertion = end
        while insertion > 0 and lines[insertion - 1] == "":
            insertion -= 1
        return lines[:insertion] + bullets + [""] + lines[insertion:]

    insertion = reference_insert_index(lines)
    block = [f"## {heading}", "", *bullets, ""]
    prefix = lines[:insertion]
    suffix = lines[insertion:]
    if prefix and prefix[-1] != "":
        prefix = prefix + [""]
    return prefix + block + suffix


def update_header(lines: list[str], key: str, value: str) -> tuple[list[str], bool]:
    needle = key.upper() + ":"
    output = list(lines)
    for index, line in enumerate(output[:40]):
        if line.upper().startswith(needle):
            output[index] = f"{key.upper()}: {value}"
            return output, True
    return output, False


def validate_plan(plan: dict[str, Any] | None, base_content: bytes) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(plan, dict) or plan.get("schema_version") != SCHEMA_VERSION:
        return None, ["update_plan_invalid"]
    if plan.get("update_plan_version") != UPDATE_PLAN_VERSION:
        return None, ["update_plan_version"]
    if plan.get("requires_merge_render") is not True:
        return None, ["merge_render_not_required"]
    if plan.get("requires_base_sha_match_before_write") is not True:
        return None, ["base_sha_guard_missing"]
    if plan.get("canonical_write_performed") is not False:
        return None, ["update_plan_write_flag"]

    base = plan.get("base_record")
    if not isinstance(base, dict):
        return None, ["base_record_missing"]
    required_base = {"record_id", "record_type", "status", "domain", "category", "title", "path", "sha256"}
    if not required_base.issubset(base):
        return None, ["base_record_fields"]
    if not isinstance(base.get("record_id"), str) or not ID_RE.fullmatch(base["record_id"]):
        return None, ["base_record_id"]
    if not isinstance(base.get("sha256"), str) or not SHA_RE.fullmatch(base["sha256"]):
        return None, ["base_record_sha"]
    actual_sha = hashlib.sha256(base_content).hexdigest()
    if actual_sha != base["sha256"]:
        return None, ["base_sha_changed"]
    path = base.get("path")
    if not isinstance(path, str) or not path.startswith("00_LIBRARY/"):
        return None, ["base_path"]

    try:
        current = validate_record(path, base_content)
    except ValueError as exc:
        return None, [f"base_record_{exc}"]
    for plan_key, current_key in (
        ("record_id", "record_id"),
        ("record_type", "record_type"),
        ("status", "status"),
        ("domain", "domain"),
        ("category", "category"),
        ("title", "title"),
    ):
        if base.get(plan_key) != current.get(current_key):
            return None, [f"base_identity_{plan_key}_mismatch"]

    proposed_status = plan.get("proposed_status")
    if proposed_status not in ALLOWED_STATUS:
        return None, ["proposed_status"]

    raw_claims = plan.get("supported_claims")
    if not isinstance(raw_claims, list) or not raw_claims:
        return None, ["supported_claims_missing"]
    claims: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, raw in enumerate(raw_claims):
        if not isinstance(raw, dict):
            errors.append(f"claim_{index}_invalid")
            continue
        claim = raw.get("claim")
        kind = raw.get("editorial_kind")
        refs = raw.get("source_refs")
        if not isinstance(claim, str) or not claim.strip():
            errors.append(f"claim_{index}_text")
            continue
        if kind not in ALLOWED_KINDS:
            errors.append(f"claim_{index}_kind")
            continue
        if not isinstance(refs, list) or not refs:
            errors.append(f"claim_{index}_source_refs")
            continue
        for ref_index, ref in enumerate(refs):
            if not isinstance(ref, dict):
                errors.append(f"claim_{index}_source_{ref_index}_invalid")
                continue
            url = ref.get("source_url")
            sha = ref.get("source_url_sha256")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                errors.append(f"claim_{index}_source_{ref_index}_url")
            if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
                errors.append(f"claim_{index}_source_{ref_index}_sha")
        claims.append({"claim": claim.strip(), "editorial_kind": kind, "source_refs": refs})
    if errors:
        return None, sorted(set(errors))

    return {
        "base": base,
        "current": current,
        "proposed_status": proposed_status,
        "claims": claims,
    }, []


def render_update(
    plan: dict[str, Any] | None,
    base_content: bytes,
) -> tuple[bytes | None, dict[str, Any] | None, list[str]]:
    validated, errors = validate_plan(plan, base_content)
    if errors or validated is None:
        return None, None, errors
    try:
        text = base_content.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, ["base_utf8"]

    had_final_newline = text.endswith("\n")
    lines = text.splitlines()
    lines, status_found = update_header(lines, "STATUS", validated["proposed_status"])
    if not status_found:
        return None, None, ["status_header_missing"]

    existing = existing_claim_keys(lines)
    grouped: dict[str, list[str]] = {}
    added = 0
    deduped = 0
    source_urls: set[str] = set()
    for item in validated["claims"]:
        claim = item["claim"]
        key = normalize_text(claim)
        for ref in item["source_refs"]:
            if isinstance(ref, dict) and isinstance(ref.get("source_url"), str):
                source_urls.add(ref["source_url"])
        if key in existing:
            deduped += 1
            continue
        heading = SECTION_TARGET[item["editorial_kind"]]
        grouped.setdefault(heading, []).append(claim)
        existing.add(key)
        added += 1

    for heading in (
        "VERIFIED CAPABILITIES",
        "COMPATIBILITY AND SETUP",
        "LIMITATIONS AND SAFETY",
        "LICENSE",
        "OTHER VERIFIED TECHNICAL NOTES",
    ):
        lines = append_to_section(lines, heading, grouped.get(heading, []))

    # Keep provenance compact. URLs are appended only when not already present,
    # and only to a dedicated verified references section.
    normalized_document = "\n".join(lines)
    missing_urls = [url for url in sorted(source_urls) if url not in normalized_document]
    if missing_urls:
        lines = append_to_section(lines, "OTHER VERIFIED TECHNICAL NOTES", [])
        ref_bounds = find_section(lines, "OTHER VERIFIED TECHNICAL NOTES")
        # Prefer an existing reference section; create VERIFIED REFERENCES if none.
        ref_index = None
        for index, line in enumerate(lines):
            match = H2_RE.match(line)
            if match and normalize_heading(match.group(1)) in {"REFERENCES", "VERIFIED REFERENCES", "SOURCES", "SOURCE"}:
                ref_index = index
                break
        if ref_index is None:
            if lines and lines[-1] != "":
                lines.append("")
            lines.extend(["## VERIFIED REFERENCES", "", *[f"- {url}" for url in missing_urls], ""])
        else:
            end = len(lines)
            for index in range(ref_index + 1, len(lines)):
                if H2_RE.match(lines[index]):
                    end = index
                    break
            insertion = end
            while insertion > ref_index and lines[insertion - 1] == "":
                insertion -= 1
            lines = lines[:insertion] + [f"- {url}" for url in missing_urls] + [""] + lines[insertion:]

    output_text = "\n".join(lines).rstrip() + ("\n" if had_final_newline or lines else "")
    output = output_text.encode("utf-8")
    if len(output) > MAX_OUTPUT_BYTES:
        return None, None, ["merged_content_too_large"]

    # Revalidate canonical identity/path after rendering. Status is allowed to
    # change to the proposed status; identity fields must remain stable.
    base = validated["base"]
    try:
        merged = validate_record(base["path"], output)
    except ValueError as exc:
        return None, None, [f"merged_record_{exc}"]
    for key in ("record_id", "record_type", "domain", "category", "title"):
        if merged[key] != base[key]:
            return None, None, [f"merged_identity_{key}_changed"]
    if merged["status"] != validated["proposed_status"]:
        return None, None, ["merged_status_mismatch"]

    metadata = {
        "schema_version": SCHEMA_VERSION,
        "merge_render_version": MERGE_RENDER_VERSION,
        "target_path": base["path"],
        "record_id": base["record_id"],
        "base_sha256": base["sha256"],
        "merged_sha256": hashlib.sha256(output).hexdigest(),
        "proposed_status": validated["proposed_status"],
        "claims_input": len(validated["claims"]),
        "claims_added": added,
        "claims_deduped": deduped,
        "requires_base_sha_match_before_write": True,
        "canonical_write_performed": False,
    }
    return output, metadata, []


def main() -> int:
    parser = argparse.ArgumentParser(description="Render one SHA-bound candidate update without publishing it.")
    parser.add_argument("--update-plan", type=Path, required=True)
    parser.add_argument("--base-record", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--metadata-out", type=Path)
    args = parser.parse_args()

    plan = read_json(args.update_plan)
    try:
        base_content = args.base_record.read_bytes()
    except OSError:
        print("candidate_update_render_error code=base_record_read_failed canonical_write=0")
        return 2
    content, metadata, errors = render_update(plan, base_content)
    if errors or content is None or metadata is None:
        print(f"candidate_update_render_error codes={','.join(errors or ['render_failed'])} canonical_write=0")
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(content)
    if args.metadata_out:
        args.metadata_out.parent.mkdir(parents=True, exist_ok=True)
        args.metadata_out.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_update_render_ok "
        f"record_id={metadata['record_id']} added={metadata['claims_added']} "
        f"deduped={metadata['claims_deduped']} base_sha_bound=1 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
