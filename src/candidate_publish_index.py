#!/usr/bin/env python3
"""Aggregate isolated candidate publish manifests into one private latest.json.

This stage never writes to 00_LIBRARY. It validates every per-candidate manifest
under 99_INBOX/CANDIDATES/PUBLISH_READY/candidate-*.json and merges them into a
single publisher-shaped latest.json in the same isolated candidate outbox.

The canonical publisher still rejects this outbox prefix. Aggregation therefore
improves determinism without weakening the publication boundary.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
INDEX_VERSION = "0.1.0"
PUBLISH_VERSION = "0.2.0"
DEFAULT_ROOT = "99_INBOX/CANDIDATES"
DEFAULT_OUTBOX = "PUBLISH_READY"
OUTBOX_PREFIX = "99_INBOX/CANDIDATES/PUBLISH_READY/records/"
MAX_MANIFESTS = 50
MAX_ITEMS = 50
MAX_MANIFEST_BYTES = 128 * 1024

ID_RE = re.compile(r"^[0-9a-f]{20}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DOMAIN_RE = re.compile(r"^(?:[0-9]{2}_[A-Z0-9_]+|SOURCES)$")
CATEGORY_RE = re.compile(r"^[A-Z0-9_]+$")
ALLOWED_TYPES = {"TECHNOLOGY", "PATTERN", "PIPELINE", "SOURCE"}
ALLOWED_STATUS = {"REFERENCE", "TEST", "DEPRECATED"}
REQUIRED_ITEM_FIELDS = {
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


def normalize_rel(path: str) -> str:
    value = path.replace("\\", "/").strip("/")
    if not value or value.startswith("../") or "/../" in f"/{value}/":
        raise ValueError("path_traversal")
    if value.startswith("./") or "//" in value:
        raise ValueError("path_invalid")
    return value


def parse_candidate_manifest(payload: dict[str, Any]) -> list[dict[str, str]]:
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
    seen_targets: set[tuple[str, str, str, str]] = set()
    for raw in items:
        if not isinstance(raw, dict) or set(raw) != REQUIRED_ITEM_FIELDS:
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

        target_key = (str(record_type), domain, category, slug)
        if target_key in seen_targets:
            raise ValueError("record_duplicate_target")
        seen_ids.add(record_id)
        seen_targets.add(target_key)
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
            }
        )
    return output


def merge_manifests(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    if len(payloads) > MAX_MANIFESTS:
        raise ValueError("too_many_manifests")
    items: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_targets: set[tuple[str, str, str, str]] = set()
    for payload in payloads:
        for item in parse_candidate_manifest(payload):
            record_id = item["record_id"]
            target = (
                item["record_type"],
                item["domain"],
                item["category"],
                item["slug"],
            )
            if record_id in seen_ids:
                raise ValueError("aggregate_duplicate_record_id")
            if target in seen_targets:
                raise ValueError("aggregate_duplicate_target")
            seen_ids.add(record_id)
            seen_targets.add(target)
            items.append(item)
    if len(items) > MAX_ITEMS:
        raise ValueError("aggregate_too_many_items")
    return {
        "schema_version": SCHEMA_VERSION,
        "publish_version": PUBLISH_VERSION,
        "items": sorted(items, key=lambda item: (item["package_id"], item["record_id"])),
    }


def read_json_bytes(raw: bytes) -> dict[str, Any]:
    if len(raw) == 0 or len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest_size")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest_json") from exc
    if not isinstance(value, dict):
        raise ValueError("manifest_json")
    return value


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["rclone", *arguments], check=False, capture_output=True)


def local_payloads(root_dir: Path, base: str) -> list[dict[str, Any]]:
    folder = root_dir / base
    if not folder.exists():
        return []
    paths = sorted(folder.glob("candidate-*.json"))[: MAX_MANIFESTS + 1]
    if len(paths) > MAX_MANIFESTS:
        raise ValueError("too_many_manifests")
    return [read_json_bytes(path.read_bytes()) for path in paths]


def remote_payloads(remote: str, base: str) -> list[dict[str, Any]]:
    listing = run_rclone([
        "lsjson",
        join_remote(remote, base),
        "--files-only",
        "--log-level",
        "ERROR",
    ])
    if listing.returncode != 0:
        # Missing outbox is equivalent to an empty candidate queue.
        return []
    try:
        rows = json.loads(listing.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest_listing") from exc
    if not isinstance(rows, list):
        raise ValueError("manifest_listing")
    names = sorted(
        {
            PurePosixPath(str(row.get("Name") or row.get("Path"))).name
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("Name") or row.get("Path"), str)
            and PurePosixPath(str(row.get("Name") or row.get("Path"))).name.startswith("candidate-")
            and PurePosixPath(str(row.get("Name") or row.get("Path"))).name.endswith(".json")
        }
    )
    if len(names) > MAX_MANIFESTS:
        raise ValueError("too_many_manifests")
    payloads: list[dict[str, Any]] = []
    for name in names:
        result = run_rclone(["cat", join_remote(remote, f"{base}/{name}"), "--log-level", "ERROR"])
        if result.returncode != 0:
            raise ValueError("manifest_read")
        payloads.append(read_json_bytes(result.stdout))
    return payloads


def write_local_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(prefix="candidate-index-", dir=path.parent, delete=False) as handle:
        handle.write(data)
        temp = Path(handle.name)
    temp.replace(path)


def write_remote_atomic(remote: str, relative: str, payload: dict[str, Any]) -> bool:
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    stage_rel = "99_INBOX/CANDIDATES/PUBLISHING/latest.json"
    with tempfile.NamedTemporaryFile(prefix="candidate-index-", delete=False) as handle:
        handle.write(data)
        local_name = handle.name
    try:
        mkdir = run_rclone([
            "mkdir",
            join_remote(remote, str(PurePosixPath(relative).parent)),
            "--log-level",
            "ERROR",
        ])
        if mkdir.returncode != 0:
            return False
        stage_mkdir = run_rclone([
            "mkdir",
            join_remote(remote, str(PurePosixPath(stage_rel).parent)),
            "--log-level",
            "ERROR",
        ])
        if stage_mkdir.returncode != 0:
            return False
        copy = run_rclone(["copyto", local_name, join_remote(remote, stage_rel), "--log-level", "ERROR"])
        if copy.returncode != 0:
            return False
        move = run_rclone([
            "moveto",
            join_remote(remote, stage_rel),
            join_remote(remote, relative),
            "--log-level",
            "ERROR",
        ])
        return move.returncode == 0
    finally:
        try:
            Path(local_name).unlink()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate isolated candidate publish manifests.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root-dir", type=Path)
    mode.add_argument("--remote")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--outbox", default=DEFAULT_OUTBOX)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    base = str(PurePosixPath(args.root) / args.outbox)
    latest = str(PurePosixPath(base) / "latest.json")
    try:
        if args.root_dir is not None:
            payloads = local_payloads(args.root_dir.resolve(), base)
        else:
            if not shutil.which("rclone"):
                print("candidate_publish_index_error code=rclone_missing canonical_write=0")
                return 2
            payloads = remote_payloads(str(args.remote), base)
        merged = merge_manifests(payloads)
    except (OSError, ValueError) as exc:
        print(f"candidate_publish_index_error code={exc} canonical_write=0")
        return 2

    if args.apply:
        if args.root_dir is not None:
            write_local_atomic(args.root_dir.resolve() / latest, merged)
        elif not write_remote_atomic(str(args.remote), latest, merged):
            print("candidate_publish_index_error code=latest_write_failed canonical_write=0")
            return 2

    mode_name = "apply" if args.apply else "dry_run"
    print(
        "candidate_publish_index_ok "
        f"mode={mode_name} manifests={len(payloads)} items={len(merged['items'])} "
        f"index_version={INDEX_VERSION} canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
