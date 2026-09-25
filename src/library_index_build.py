#!/usr/bin/env python3
"""Build deterministic routing indexes from canonical raw Markdown records.

Only canonical filenames produced by the publisher are considered:
  technology-*.md, pattern-*.md, pipeline-*.md, source-*.md

The builder validates record ownership/path metadata, detects duplicate RECORD_IDs and
same-type/same-title canonical duplicates, then renders MASTER_INDEX.md plus one local
INDEX.md per populated domain. It is dry-run by default and supports local fixtures or
an rclone remote.

Native Google Docs are deliberately ignored because their names do not match the raw
canonical filename contract. This lets the raw-file migration be validated without
silently treating old editor documents as authoritative bytes.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

RECORD_NAME_RE = re.compile(
    r"^(technology|pattern|pipeline|source)-[a-z0-9]+(?:-[a-z0-9]+)*\.md$"
)
ID_RE = re.compile(r"^[0-9a-f]{20}$")
DOMAIN_RE = re.compile(r"^(?:[0-9]{2}_[A-Z0-9_]+|SOURCES)$")
CATEGORY_RE = re.compile(r"^[A-Z0-9_]+$")
ALLOWED_TYPES = {"TECHNOLOGY", "PATTERN", "PIPELINE", "SOURCE"}
ALLOWED_STATUS = {"REFERENCE", "TEST", "DEPRECATED"}
MAX_RECORD_BYTES = 128 * 1024


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *arguments], check=False, capture_output=True, text=True
    )


def read_local(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def read_remote(path: str) -> bytes | None:
    result = subprocess.run(
        ["rclone", "cat", path, "--log-level", "ERROR"],
        check=False,
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else None


def parse_headers(content: bytes) -> dict[str, str]:
    if not content or len(content) > MAX_RECORD_BYTES:
        raise ValueError("record_size")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("record_utf8") from exc
    headers: dict[str, str] = {}
    for line in text.splitlines()[:40]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().upper()
        if key in {"RECORD_ID", "TYPE", "STATUS", "DOMAIN", "CATEGORY", "TITLE"}:
            headers[key] = value.strip()
    return headers


def expected_path(headers: dict[str, str], slug: str) -> str:
    record_type = headers["TYPE"]
    domain = headers["DOMAIN"]
    category = headers["CATEGORY"]
    filename = f"{record_type.lower()}-{slug}.md"
    if record_type == "SOURCE":
        return f"00_LIBRARY/SOURCES/{filename}"
    return f"00_LIBRARY/{domain}/{category}/{filename}"


def validate_record(path: str, content: bytes) -> dict[str, str]:
    headers = parse_headers(content)
    required = {"RECORD_ID", "TYPE", "STATUS", "DOMAIN", "CATEGORY", "TITLE"}
    if not required.issubset(headers):
        raise ValueError("record_headers")
    if not ID_RE.fullmatch(headers["RECORD_ID"]):
        raise ValueError("record_id")
    if headers["TYPE"] not in ALLOWED_TYPES:
        raise ValueError("record_type")
    if headers["STATUS"] not in ALLOWED_STATUS:
        raise ValueError("record_status")
    if not DOMAIN_RE.fullmatch(headers["DOMAIN"]):
        raise ValueError("record_domain")
    if not CATEGORY_RE.fullmatch(headers["CATEGORY"]):
        raise ValueError("record_category")
    if not headers["TITLE"] or len(headers["TITLE"]) > 160:
        raise ValueError("record_title")
    if headers["TYPE"] == "SOURCE" and headers["DOMAIN"] != "SOURCES":
        raise ValueError("source_domain")
    if headers["TYPE"] != "SOURCE" and headers["DOMAIN"] == "SOURCES":
        raise ValueError("canonical_domain")

    name = Path(path).name
    match = RECORD_NAME_RE.fullmatch(name)
    if not match:
        raise ValueError("record_filename")
    type_from_name = match.group(1).upper()
    if type_from_name != headers["TYPE"]:
        raise ValueError("record_filename_type")
    slug = name[len(match.group(1)) + 1 : -3]
    if expected_path(headers, slug) != path:
        raise ValueError("record_path")
    return {
        "record_id": headers["RECORD_ID"],
        "record_type": headers["TYPE"],
        "status": headers["STATUS"],
        "domain": headers["DOMAIN"],
        "category": headers["CATEGORY"],
        "title": headers["TITLE"],
        "path": path,
    }


def local_record_paths(root: Path) -> list[str]:
    library = root / "00_LIBRARY"
    if not library.exists():
        return []
    output: list[str] = []
    for path in library.rglob("*.md"):
        if path.is_file() and RECORD_NAME_RE.fullmatch(path.name):
            output.append(path.relative_to(root).as_posix())
    return sorted(output)


def remote_record_paths(remote: str) -> list[str]:
    result = run_rclone(
        [
            "lsjson",
            join_remote(remote, "00_LIBRARY"),
            "--recursive",
            "--files-only",
            "--log-level",
            "ERROR",
        ]
    )
    if result.returncode != 0:
        return []
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    output: list[str] = []
    if not isinstance(rows, list):
        return output
    for row in rows:
        if not isinstance(row, dict):
            continue
        rel = row.get("Path")
        if not isinstance(rel, str):
            continue
        rel = rel.replace("\\", "/").strip("/")
        name = Path(rel).name
        if RECORD_NAME_RE.fullmatch(name):
            output.append(f"00_LIBRARY/{rel}")
    return sorted(set(output))


def load_records(root: Path | None, remote: str | None) -> list[dict[str, str]]:
    paths = local_record_paths(root) if root is not None else remote_record_paths(str(remote))
    records: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_titles: set[tuple[str, str]] = set()
    for path in paths:
        raw = read_local(root / path) if root is not None else read_remote(join_remote(str(remote), path))
        if raw is None:
            raise ValueError(f"record_missing:{path}")
        try:
            record = validate_record(path, raw)
        except ValueError as exc:
            raise ValueError(f"{exc}:{path}") from exc
        if record["record_id"] in seen_ids:
            raise ValueError(f"duplicate_record_id:{record['record_id']}")
        title_key = (record["record_type"], record["title"].casefold())
        if title_key in seen_titles:
            raise ValueError(f"duplicate_canonical_title:{record['record_type']}:{record['title']}")
        seen_ids.add(record["record_id"])
        seen_titles.add(title_key)
        records.append(record)
    return sorted(records, key=lambda r: (r["domain"], r["category"], r["record_type"], r["title"].casefold()))


def render_master(records: list[dict[str, str]]) -> bytes:
    lines = [
        "# MASTER_INDEX",
        "",
        "GENERATED: TRUE",
        f"RECORDS: {len(records)}",
        "",
        "Canonical raw-record routing index. Generated from validated record headers; do not hand-edit.",
        "",
    ]
    by_domain: dict[str, list[dict[str, str]]] = {}
    for record in records:
        by_domain.setdefault(record["domain"], []).append(record)
    for domain in sorted(by_domain):
        lines.extend([f"## {domain}", ""])
        categories: dict[str, list[dict[str, str]]] = {}
        for record in by_domain[domain]:
            categories.setdefault(record["category"], []).append(record)
        for category in sorted(categories):
            lines.extend([f"### {category}", ""])
            for record in categories[category]:
                lines.append(
                    f"- {record['record_type']} - {record['title']} [{record['status']}] (`{record['path']}`)"
                )
            lines.append("")
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def render_local(domain: str, records: list[dict[str, str]]) -> bytes:
    lines = [
        f"# INDEX — {domain}",
        "",
        "GENERATED: TRUE",
        f"RECORDS: {len(records)}",
        "",
        "Generated from canonical raw Markdown records. Do not hand-edit.",
        "",
    ]
    categories: dict[str, list[dict[str, str]]] = {}
    for record in records:
        categories.setdefault(record["category"], []).append(record)
    for category in sorted(categories):
        lines.extend([f"## {category}", ""])
        for record in categories[category]:
            lines.append(
                f"- {record['record_type']} - {record['title']} [{record['status']}] (`{Path(record['path']).name}`)"
            )
        lines.append("")
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def planned_indexes(records: list[dict[str, str]]) -> dict[str, bytes]:
    output = {"00_LIBRARY/MASTER_INDEX.md": render_master(records)}
    domains: dict[str, list[dict[str, str]]] = {}
    for record in records:
        domains.setdefault(record["domain"], []).append(record)
    for domain, items in domains.items():
        output[f"00_LIBRARY/{domain}/INDEX.md"] = render_local(domain, items)
    return output


def write_local_atomic(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".index-", dir=str(target.parent))
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


def write_remote_atomic(remote: str, target_rel: str, content: bytes) -> None:
    stage_rel = "99_INBOX/CURATION/INDEX_BUILD/" + target_rel.replace("/", "__")
    with tempfile.NamedTemporaryFile(prefix="tl-index-", suffix=".md", delete=False) as handle:
        handle.write(content)
        local_name = handle.name
    try:
        copy = run_rclone(["copyto", local_name, join_remote(remote, stage_rel), "--log-level", "ERROR"])
        if copy.returncode != 0:
            raise RuntimeError("index_stage_failed")
        move = run_rclone(["moveto", join_remote(remote, stage_rel), join_remote(remote, target_rel), "--log-level", "ERROR"])
        if move.returncode != 0:
            raise RuntimeError("index_write_failed")
    finally:
        try:
            os.unlink(local_name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Build canonical Technology Library indexes.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root-dir", type=Path)
    mode.add_argument("--remote")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    root = args.root_dir.resolve() if args.root_dir is not None else None
    try:
        records = load_records(root, str(args.remote) if args.remote is not None else None)
    except ValueError as exc:
        print(f"library_index_build_error code={exc}")
        return 2
    if not records:
        print("library_index_build_skip reason=no_records")
        return 0

    indexes = planned_indexes(records)
    planned = 0
    updated = 0
    unchanged = 0
    for path, content in sorted(indexes.items()):
        existing = read_local(root / path) if root is not None else read_remote(join_remote(str(args.remote), path))
        if existing == content:
            unchanged += 1
            continue
        planned += 1
        if not args.apply:
            continue
        try:
            if root is not None:
                write_local_atomic(root / path, content)
            else:
                write_remote_atomic(str(args.remote), path, content)
        except RuntimeError as exc:
            print(f"library_index_build_error code={exc} path={path}")
            return 2
        updated += 1

    mode_name = "apply" if args.apply else "dry_run"
    print(
        "library_index_build_ok "
        f"mode={mode_name} records={len(records)} indexes={len(indexes)} "
        f"planned={planned} updated={updated} unchanged={unchanged}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
