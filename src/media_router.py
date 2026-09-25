#!/usr/bin/env python3
"""Mechanical routing for the Technology Library unified private inbox.

Users place any supported file in 99_INBOX/DROP_HERE. The router classifies it
using specific provider MIME metadata and known extensions before falling back
to broad/generic MIME families. It performs no semantic analysis, never
overwrites an existing file, and never prints private filenames in normal CI
output.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_SOURCE = "99_INBOX/DROP_HERE"
DEFAULT_VIDEOS = "99_INBOX/TO_REVIEW/VIDEOS"
DEFAULT_IMAGES = "99_INBOX/TO_REVIEW/IMAGES"
DEFAULT_AUDIO = "99_INBOX/TO_REVIEW/AUDIO"
DEFAULT_DOCUMENTS = "99_INBOX/TO_REVIEW/DOCUMENTS"
DEFAULT_SPREADSHEETS = "99_INBOX/TO_REVIEW/SPREADSHEETS"
DEFAULT_TEXT = "99_INBOX/TO_REVIEW/TEXT"
DEFAULT_LINKS = "99_INBOX/TO_REVIEW/LINKS"
DEFAULT_ARCHIVES = "99_INBOX/TO_REVIEW/ARCHIVES"
DEFAULT_UNKNOWN = "99_INBOX/ERROR/UNSORTED"

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif", ".tif", ".tiff"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"}
SPREADSHEET_SUFFIXES = {".csv", ".tsv", ".xlsx", ".xls", ".xlsm", ".ods"}
TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".yaml", ".yml", ".xml",
    ".html", ".htm", ".log", ".ini", ".cfg", ".conf", ".toml", ".env",
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".css",
    ".scss", ".sass", ".less", ".sql", ".sh", ".bash", ".zsh", ".ps1",
    ".bat", ".cmd", ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".java",
    ".kt", ".kts", ".go", ".rs", ".rb", ".php", ".swift", ".dart", ".lua",
    ".r", ".scala", ".vue", ".svelte", ".graphql", ".gql", ".proto",
}
LINK_SUFFIXES = {".url", ".webloc"}
ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz"}
DOCUMENT_SUFFIXES = {
    ".pdf", ".docx", ".doc", ".odt", ".rtf",
    ".pptx", ".ppt", ".odp",
    ".dwg", ".dxf", ".ifc", ".rvt",
}

TEXT_MIME_TYPES = {
    "application/json", "application/ld+json", "application/xml",
    "application/yaml", "application/x-yaml", "application/javascript",
    "application/x-javascript", "application/toml", "application/sql",
}
SPREADSHEET_MIME_TYPES = {
    "text/csv", "text/tab-separated-values",
    "application/vnd.ms-excel",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",
}
DOCUMENT_MIME_TYPES = {
    "application/pdf", "application/msword", "application/rtf",
    "application/vnd.ms-powerpoint",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.presentation",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
ARCHIVE_MIME_TYPES = {
    "application/zip", "application/x-7z-compressed", "application/vnd.rar",
    "application/x-rar-compressed", "application/x-tar", "application/gzip",
    "application/x-gzip", "application/x-bzip2", "application/x-xz",
}
LINK_MIME_TYPES = {
    "application/internet-shortcut",
    "application/x-url",
    "application/x-webloc",
}

TARGETS = {
    "video": DEFAULT_VIDEOS,
    "image": DEFAULT_IMAGES,
    "audio": DEFAULT_AUDIO,
    "document": DEFAULT_DOCUMENTS,
    "spreadsheet": DEFAULT_SPREADSHEETS,
    "text": DEFAULT_TEXT,
    "link": DEFAULT_LINKS,
    "archive": DEFAULT_ARCHIVES,
    "unknown": DEFAULT_UNKNOWN,
}


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    # rclone emits UTF-8 JSON even when Windows uses a legacy console codepage.
    # Keep subprocess output as bytes so Python never decodes it as cp1252.
    return subprocess.run(["rclone", *arguments], check=False, capture_output=True)


def ensure_remote_dir(remote_root: str, relative: str) -> bool:
    return run_rclone(["mkdir", join_remote(remote_root, relative), "--log-level", "ERROR"]).returncode == 0


def classify_item(item: dict[str, Any]) -> str:
    name = item.get("Name") or item.get("Path")
    suffix = Path(name).suffix.lower() if isinstance(name, str) else ""
    mime = str(item.get("MimeType") or "").lower().strip()

    # Strong MIME families and specific application MIME types are reliable.
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime in LINK_MIME_TYPES:
        return "link"
    if mime in SPREADSHEET_MIME_TYPES:
        return "spreadsheet"
    if mime in DOCUMENT_MIME_TYPES:
        return "document"
    if mime in TEXT_MIME_TYPES:
        return "text"

    # Specific extensions beat broad MIME labels such as application/zip or
    # text/plain. OOXML files are ZIP containers and some providers expose only
    # that generic MIME, which must not reroute XLSX/DOCX/PPTX to ARCHIVES.
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in LINK_SUFFIXES:
        return "link"
    if suffix in SPREADSHEET_SUFFIXES:
        return "spreadsheet"
    if suffix in DOCUMENT_SUFFIXES:
        return "document"
    if suffix in TEXT_SUFFIXES:
        return "text"
    if suffix in ARCHIVE_SUFFIXES:
        return "archive"

    # Broad MIME families are only fallbacks once specific extension knowledge
    # has had a chance to disambiguate the file.
    if mime in ARCHIVE_MIME_TYPES:
        return "archive"
    if mime.startswith("text/"):
        return "text"
    return "unknown"


def list_items(remote_root: str, source: str) -> list[dict[str, Any]] | None:
    result = run_rclone(["lsjson", join_remote(remote_root, source), "--files-only", "--log-level", "ERROR"])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else None


def item_name(item: dict[str, Any]) -> str | None:
    raw = item.get("Name") or item.get("Path")
    if not isinstance(raw, str) or not raw:
        return None
    return PurePosixPath(raw).name


def item_remote_path(remote_root: str, folder: str, item: dict[str, Any]) -> str | None:
    name = item.get("Path") or item.get("Name")
    if not isinstance(name, str) or not name:
        return None
    return join_remote(remote_root, str(PurePosixPath(folder) / name))


def remote_exists(remote_path: str) -> bool:
    return run_rclone(["lsjson", remote_path, "--stat", "--log-level", "ERROR"]).returncode == 0


def collision_token(item: dict[str, Any]) -> str:
    provider_id = item.get("ID")
    basis = provider_id if isinstance(provider_id, str) and provider_id else "|".join([
        str(item.get("Name") or item.get("Path") or ""),
        str(item.get("Size") or ""),
        str(item.get("ModTime") or ""),
    ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:10]


def destination_candidates(remote_root: str, folder: str, item: dict[str, Any]) -> list[str]:
    name = item_name(item)
    if not name:
        return []
    path = PurePosixPath(name)
    normal = join_remote(remote_root, str(PurePosixPath(folder) / path.name))
    alternate_name = f"{path.stem}__{collision_token(item)}{path.suffix}"
    alternate = join_remote(remote_root, str(PurePosixPath(folder) / alternate_name))
    return [normal, alternate]


def move_without_overwrite(source: str, destinations: list[str]) -> str:
    for index, destination in enumerate(destinations):
        if remote_exists(destination):
            continue
        result = run_rclone(["moveto", source, destination, "--log-level", "ERROR", "--stats", "0"])
        if result.returncode == 0:
            return "moved" if index == 0 else "renamed"
        return "error"
    return "collision"


def route_media(remote_root: str, source: str = DEFAULT_SOURCE, targets: dict[str, str] | None = None) -> tuple[bool, dict[str, int]]:
    routes = dict(TARGETS if targets is None else targets)
    stats = {
        "scanned": 0, "videos": 0, "images": 0, "audio": 0, "documents": 0,
        "spreadsheets": 0, "text": 0, "links": 0, "archives": 0, "unknown": 0,
        "renamed": 0, "collisions": 0, "errors": 0,
    }
    for folder in {source, *routes.values()}:
        if not ensure_remote_dir(remote_root, folder):
            stats["errors"] += 1
            return False, stats

    items = list_items(remote_root, source)
    if items is None:
        stats["errors"] += 1
        return False, stats

    stat_key = {
        "video": "videos", "image": "images", "audio": "audio",
        "document": "documents", "spreadsheet": "spreadsheets", "text": "text",
        "link": "links", "archive": "archives", "unknown": "unknown",
    }
    for item in items:
        stats["scanned"] += 1
        kind = classify_item(item)
        source_path = item_remote_path(remote_root, source, item)
        destinations = destination_candidates(remote_root, routes[kind], item)
        if not source_path or not destinations:
            stats["errors"] += 1
            continue
        outcome = move_without_overwrite(source_path, destinations)
        if outcome == "error":
            stats["errors"] += 1
            continue
        if outcome == "collision":
            stats["collisions"] += 1
            continue
        if outcome == "renamed":
            stats["renamed"] += 1
        stats[stat_key[kind]] += 1

    return stats["errors"] == 0 and stats["collisions"] == 0, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Route files from one private inbox to internal media queues.")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("router_error code=rclone_missing")
        return 2

    ok, stats = route_media(args.remote, args.source)
    if not ok:
        print(
            "router_error code=routing_failed "
            f"scanned={stats['scanned']} errors={stats['errors']} collisions={stats['collisions']}"
        )
        return 2

    print(
        "router_ok "
        f"scanned={stats['scanned']} videos={stats['videos']} images={stats['images']} "
        f"audio={stats['audio']} documents={stats['documents']} spreadsheets={stats['spreadsheets']} "
        f"text={stats['text']} links={stats['links']} archives={stats['archives']} "
        f"unknown={stats['unknown']} renamed={stats['renamed']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
