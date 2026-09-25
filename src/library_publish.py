#!/usr/bin/env python3
"""Publish validated raw Markdown records from private curation storage.

The publisher intentionally reads semantic record content only from private storage.
GitHub remains a control plane: no card body, transcript, OCR text, filename or URL is
required in the repository.

Publishing is deterministic and fail-closed:
- manifest paths are constrained to the private publish outbox;
- every record is bound to a source package_id + revision_key;
- target paths are derived, never supplied by the manifest;
- content SHA-256 and metadata headers must match the manifest;
- an existing target owned by another RECORD_ID is never overwritten;
- identical bytes are idempotent no-ops;
- remote writes stage to a temporary object before moveto;
- a publish receipt is written only after every target is verified byte-for-byte.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
PUBLISH_VERSION = "0.2.0"
PUBLISH_STATE_VERSION = "0.1.0"
DEFAULT_MANIFEST = "99_INBOX/CURATION/PUBLISH_READY/latest.json"
DEFAULT_RECEIPT = "99_INBOX/CURATION/PUBLISH_STATE/latest.json"
OUTBOX_PREFIX = "99_INBOX/CURATION/PUBLISH_READY/records/"
MAX_ITEMS = 50
MAX_CONTENT_BYTES = 128 * 1024

ID_RE = re.compile(r"^[0-9a-f]{20}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DOMAIN_RE = re.compile(r"^(?:[0-9]{2}_[A-Z0-9_]+|SOURCES)$")
CATEGORY_RE = re.compile(r"^[A-Z0-9_]+$")
ALLOWED_TYPES = {"TECHNOLOGY", "PATTERN", "PIPELINE", "SOURCE"}
ALLOWED_STATUS = {"REFERENCE", "TEST", "DEPRECATED"}


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *arguments], check=False, capture_output=True, text=True
    )


def read_local(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def read_remote(remote_path: str) -> bytes | None:
    result = subprocess.run(
        ["rclone", "cat", remote_path, "--log-level", "ERROR"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def read_json_bytes(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def normalize_rel(path: str) -> str:
    value = path.replace("\\", "/").strip("/")
    if not value or value.startswith("../") or "/../" in f"/{value}/":
        raise ValueError("path_traversal")
    if value.startswith("./") or "//" in value:
        raise ValueError("path_invalid")
    return value


def parse_manifest(payload: dict[str, Any]) -> list[dict[str, str]]:
    if set(payload) != {"schema_version", "publish_version", "items"}:
        raise ValueError("manifest_top_level_fields")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("manifest_schema")
    if payload.get("publish_version") != PUBLISH_VERSION:
        raise ValueError("manifest_version")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ValueError("manifest_items")

    output: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_targets: set[str] = set()
    required = {
        "package_id",
        "revision_key",
        "record_id",
        "record_type",
        "status",
        "domain",
        "category",
        "slug",
        "title",
        "content_path",
        "content_sha256",
    }
    for raw in items:
        if not isinstance(raw, dict) or set(raw) != required:
            raise ValueError("record_fields")
        package_id = raw.get("package_id")
        revision_key = raw.get("revision_key")
        record_id = raw.get("record_id")
        record_type = raw.get("record_type")
        status = raw.get("status")
        domain = raw.get("domain")
        category = raw.get("category")
        slug = raw.get("slug")
        title = raw.get("title")
        content_path = raw.get("content_path")
        content_sha = raw.get("content_sha256")

        if not isinstance(package_id, str) or not ID_RE.fullmatch(package_id):
            raise ValueError("record_package_id")
        if not isinstance(revision_key, str) or not ID_RE.fullmatch(revision_key):
            raise ValueError("record_revision_key")
        if not isinstance(record_id, str) or not ID_RE.fullmatch(record_id):
            raise ValueError("record_id")
        if record_id in seen_ids:
            raise ValueError("record_duplicate_id")
        if record_type not in ALLOWED_TYPES:
            raise ValueError("record_type")
        if status not in ALLOWED_STATUS:
            raise ValueError("record_status")
        if not isinstance(domain, str) or not DOMAIN_RE.fullmatch(domain):
            raise ValueError("record_domain")
        if not isinstance(category, str) or not CATEGORY_RE.fullmatch(category):
            raise ValueError("record_category")
        if record_type == "SOURCE" and domain != "SOURCES":
            raise ValueError("source_domain")
        if record_type != "SOURCE" and domain == "SOURCES":
            raise ValueError("canonical_domain")
        if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
            raise ValueError("record_slug")
        if not isinstance(title, str) or not title.strip() or len(title) > 160:
            raise ValueError("record_title")
        if not isinstance(content_sha, str) or not SHA_RE.fullmatch(content_sha):
            raise ValueError("record_sha256")
        if not isinstance(content_path, str):
            raise ValueError("record_content_path")
        content_path = normalize_rel(content_path)
        if not content_path.startswith(OUTBOX_PREFIX) or not content_path.endswith(".md"):
            raise ValueError("record_content_path")

        target = derive_target(
            record_type=str(record_type),
            domain=domain,
            category=category,
            slug=slug,
        )
        if target in seen_targets:
            raise ValueError("record_duplicate_target")
        seen_ids.add(record_id)
        seen_targets.add(target)
        output.append(
            {
                "package_id": package_id,
                "revision_key": revision_key,
                "record_id": record_id,
                "record_type": str(record_type),
                "status": str(status),
                "domain": domain,
                "category": category,
                "slug": slug,
                "title": title.strip(),
                "content_path": content_path,
                "content_sha256": content_sha,
                "target_path": target,
            }
        )
    return output


def derive_target(*, record_type: str, domain: str, category: str, slug: str) -> str:
    filename = f"{record_type.lower()}-{slug}.md"
    if record_type == "SOURCE":
        return f"00_LIBRARY/SOURCES/{filename}"
    return f"00_LIBRARY/{domain}/{category}/{filename}"


def parse_headers(content: bytes) -> dict[str, str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("content_utf8") from exc
    headers: dict[str, str] = {}
    for line in text.splitlines()[:40]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().upper()
        if key in {"RECORD_ID", "TYPE", "STATUS", "DOMAIN", "CATEGORY", "TITLE"}:
            headers[key] = value.strip()
    return headers


def validate_content(item: dict[str, str], content: bytes) -> None:
    if len(content) == 0 or len(content) > MAX_CONTENT_BYTES:
        raise ValueError("content_size")
    digest = hashlib.sha256(content).hexdigest()
    if digest != item["content_sha256"]:
        raise ValueError("content_sha256")
    headers = parse_headers(content)
    expected = {
        "RECORD_ID": item["record_id"],
        "TYPE": item["record_type"],
        "STATUS": item["status"],
        "DOMAIN": item["domain"],
        "CATEGORY": item["category"],
        "TITLE": item["title"],
    }
    for key, value in expected.items():
        if headers.get(key) != value:
            raise ValueError(f"content_header_{key.lower()}")


def target_owner(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    try:
        headers = parse_headers(raw)
    except ValueError:
        return None
    value = headers.get("RECORD_ID")
    return value if isinstance(value, str) and ID_RE.fullmatch(value) else None


def write_local_atomic(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".publish-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def write_remote_atomic(remote: str, target_rel: str, stage_name: str, content: bytes) -> None:
    temp_rel = f"99_INBOX/CURATION/PUBLISHING/{stage_name}"
    with tempfile.NamedTemporaryFile(prefix="tl-publish-", delete=False) as handle:
        handle.write(content)
        local_name = handle.name
    try:
        copy = run_rclone(
            ["copyto", local_name, join_remote(remote, temp_rel), "--log-level", "ERROR"]
        )
        if copy.returncode != 0:
            raise RuntimeError("remote_stage_failed")
        move = run_rclone(
            [
                "moveto",
                join_remote(remote, temp_rel),
                join_remote(remote, target_rel),
                "--log-level",
                "ERROR",
            ]
        )
        if move.returncode != 0:
            raise RuntimeError("remote_publish_failed")
    finally:
        try:
            os.unlink(local_name)
        except FileNotFoundError:
            pass


def receipt_bytes(items: list[dict[str, str]]) -> bytes:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "publish_state_version": PUBLISH_STATE_VERSION,
        "publish_version": PUBLISH_VERSION,
        "items": [
            {
                "package_id": item["package_id"],
                "revision_key": item["revision_key"],
                "record_id": item["record_id"],
                "target_path": item["target_path"],
                "content_sha256": item["content_sha256"],
            }
            for item in sorted(items, key=lambda row: (row["package_id"], row["record_id"]))
        ],
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish private semantic curation records.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root-dir", type=Path)
    mode.add_argument("--remote")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--receipt", default=DEFAULT_RECEIPT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    try:
        manifest_rel = normalize_rel(args.manifest)
        receipt_rel = normalize_rel(args.receipt)
    except ValueError as exc:
        print(f"library_publish_error code={exc}")
        return 2

    if args.root_dir is not None:
        root = args.root_dir.resolve()
        manifest_raw = read_local(root / manifest_rel)
    else:
        root = None
        manifest_raw = read_remote(join_remote(str(args.remote), manifest_rel))

    if manifest_raw is None:
        print("library_publish_skip reason=manifest_missing")
        return 0

    payload = read_json_bytes(manifest_raw)
    if payload is None:
        print("library_publish_error code=manifest_invalid")
        return 2
    try:
        items = parse_manifest(payload)
    except ValueError as exc:
        print(f"library_publish_error code={exc}")
        return 2

    planned = 0
    unchanged = 0
    updated = 0
    expected_content: dict[str, bytes] = {}
    for item in items:
        if root is not None:
            content = read_local(root / item["content_path"])
            existing = read_local(root / item["target_path"])
        else:
            content = read_remote(join_remote(str(args.remote), item["content_path"]))
            existing = read_remote(join_remote(str(args.remote), item["target_path"]))
        if content is None:
            print(f"library_publish_error code=content_missing record_id={item['record_id']}")
            return 2
        try:
            validate_content(item, content)
        except ValueError as exc:
            print(f"library_publish_error code={exc} record_id={item['record_id']}")
            return 2
        expected_content[item["target_path"]] = content

        if existing == content:
            unchanged += 1
            continue
        owner = target_owner(existing)
        if existing is not None and owner != item["record_id"]:
            print(
                "library_publish_error code=target_collision "
                f"record_id={item['record_id']} target={item['target_path']}"
            )
            return 2
        planned += 1
        if not args.apply:
            continue
        try:
            if root is not None:
                write_local_atomic(root / item["target_path"], content)
            else:
                write_remote_atomic(
                    str(args.remote),
                    item["target_path"],
                    f"record-{item['record_id']}.md",
                    content,
                )
        except RuntimeError as exc:
            print(f"library_publish_error code={exc} record_id={item['record_id']}")
            return 2
        updated += 1

    if args.apply:
        for item in items:
            if root is not None:
                actual = read_local(root / item["target_path"])
            else:
                actual = read_remote(join_remote(str(args.remote), item["target_path"]))
            if actual != expected_content[item["target_path"]]:
                print(
                    "library_publish_error code=target_verify_failed "
                    f"record_id={item['record_id']}"
                )
                return 2
        receipt = receipt_bytes(items)
        try:
            if root is not None:
                write_local_atomic(root / receipt_rel, receipt)
            else:
                write_remote_atomic(
                    str(args.remote), receipt_rel, "publish-receipt.json", receipt
                )
        except RuntimeError as exc:
            print(f"library_publish_error code={exc}")
            return 2

    mode_name = "apply" if args.apply else "dry_run"
    print(
        "library_publish_ok "
        f"mode={mode_name} items={len(items)} planned={planned} "
        f"updated={updated} unchanged={unchanged} receipt={1 if args.apply else 0}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
