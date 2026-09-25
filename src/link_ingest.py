#!/usr/bin/env python3
"""Offline-safe link ingestion for Technology Library.

Link V1 parses Windows .url and Apple .webloc shortcut files locally. It never
performs network requests. Full URLs remain private package data while compact
evidence removes query strings and fragments to reduce accidental token/secret
exposure during later curation.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import plistlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

LINK_PIPELINE_VERSION = "0.1.0"
LINK_SUMMARY_VERSION = "0.1.0"
SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 1024 * 1024
MAX_URL_CHARS = 8192
MAX_SUMMARY_BYTES = 3072
MAX_SUMMARY_PATH_CHARS = 768
SUPPORTED_SUFFIXES = {".url", ".webloc"}
ALLOWED_SCHEMES = {"http", "https"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, payload: dict[str, Any], *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    else:
        rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(rendered, encoding="utf-8")


def parse_windows_url(path: Path) -> str:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = path.read_text(encoding="cp1252")
        except UnicodeDecodeError as exc:
            raise RuntimeError("link_decode_failed") from exc
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise RuntimeError("link_parse_failed") from exc
    if not parser.has_section("InternetShortcut"):
        raise RuntimeError("link_url_missing")
    value = parser.get("InternetShortcut", "URL", fallback="").strip()
    if not value:
        raise RuntimeError("link_url_missing")
    return value


def parse_webloc(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (plistlib.InvalidFileException, ValueError, OSError) as exc:
        raise RuntimeError("link_parse_failed") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("link_url_missing")
    value = payload.get("URL")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("link_url_missing")
    return value.strip()


def extract_url(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".url":
        return parse_windows_url(path)
    if suffix == ".webloc":
        return parse_webloc(path)
    raise RuntimeError("unsupported_link_format")


def normalized_host(parsed: SplitResult) -> str:
    hostname = parsed.hostname
    if not hostname:
        raise RuntimeError("link_host_missing")
    try:
        return hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise RuntimeError("link_host_invalid") from exc


def normalize_url(raw_url: str) -> dict[str, Any]:
    url = raw_url.strip()
    if not url or len(url) > MAX_URL_CHARS:
        raise RuntimeError("link_url_invalid")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise RuntimeError("link_url_invalid") from exc

    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise RuntimeError("link_scheme_not_allowed")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError("link_userinfo_not_allowed")

    host = normalized_host(parsed)
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("link_port_invalid") from exc

    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path or "/"
    normalized = urlunsplit((scheme, netloc, path, parsed.query, parsed.fragment))
    safe_path = path[:MAX_SUMMARY_PATH_CHARS]
    safe_url = urlunsplit((scheme, netloc, safe_path, "", ""))

    return {
        "url": url,
        "normalized_url": normalized,
        "summary_url": safe_url,
        "scheme": scheme,
        "host": host,
        "port": None if default_port else port,
        "path": path,
        "has_query": bool(parsed.query),
        "has_fragment": bool(parsed.fragment),
    }


def ingest_link(source: Path, out: Path, *, source_id: str | None) -> dict[str, Any]:
    if not source.is_file():
        raise RuntimeError("source_missing")
    if source.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise RuntimeError("unsupported_link_format")
    size = source.stat().st_size
    if size < 1:
        raise RuntimeError("source_empty")
    if size > MAX_INPUT_BYTES:
        raise RuntimeError("source_too_large")

    out.mkdir(parents=True, exist_ok=True)
    source_sha = sha256_file(source)
    extracted = extract_url(source)
    parsed = normalize_url(extracted)

    index = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": LINK_PIPELINE_VERSION,
        "source": {"sha256": source_sha, "bytes": size, "format": source.suffix.lower().lstrip(".")},
        "link": {
            "url": parsed["url"],
            "normalized_url": parsed["normalized_url"],
            "scheme": parsed["scheme"],
            "host": parsed["host"],
            "port": parsed["port"],
            "path": parsed["path"],
            "has_query": parsed["has_query"],
            "has_fragment": parsed["has_fragment"],
        },
        "network": {
            "fetched": False,
            "policy": "offline_v1",
        },
    }
    write_json(out / "link-index.json", index)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": LINK_PIPELINE_VERSION,
        "created_at": utc_now(),
        "source": {
            "provider": "external",
            "file_id": source_id,
            "sha256": source_sha,
            "bytes": size,
        },
        "link": {
            "scheme": parsed["scheme"],
            "host": parsed["host"],
            "has_query": parsed["has_query"],
            "has_fragment": parsed["has_fragment"],
        },
        "artifacts": {"index": "link-index.json"},
        "network": {"fetched": False, "policy": "offline_v1"},
    }
    write_json(out / "ingest.json", manifest)

    summary = {
        "schema_version": SCHEMA_VERSION,
        "summary_version": LINK_SUMMARY_VERSION,
        "source": {
            "provider": "external",
            "file_id": source_id,
            "sha256": source_sha,
        },
        "link": {
            "url_without_query_or_fragment": parsed["summary_url"],
            "scheme": parsed["scheme"],
            "host": parsed["host"],
            "has_query": parsed["has_query"],
            "has_fragment": parsed["has_fragment"],
        },
        "network": {
            "fetched": False,
            "policy": "offline_v1",
        },
        "audit": {
            "manifest": "ingest.json",
            "index": "link-index.json",
        },
    }
    write_json(out / "evidence-summary.json", summary, compact=True)
    if (out / "evidence-summary.json").stat().st_size > MAX_SUMMARY_BYTES:
        raise RuntimeError("summary_budget_exceeded")

    checkpoint = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": LINK_PIPELINE_VERSION,
        "summary_version": LINK_SUMMARY_VERSION,
        "source_sha256": source_sha,
        "status": "complete",
        "required_artifacts": [
            "ingest.json",
            "checkpoint.json",
            "link-index.json",
            "evidence-summary.json",
        ],
    }
    write_json(out / "checkpoint.json", checkpoint)

    return {
        "scheme": parsed["scheme"],
        "host_hash": hashlib.sha256(parsed["host"].encode("utf-8")).hexdigest()[:12],
        "has_query": parsed["has_query"],
        "has_fragment": parsed["has_fragment"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create offline compact evidence from a .url or .webloc shortcut.")
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-id")
    args = parser.parse_args()

    try:
        result = ingest_link(args.source.resolve(), args.out.resolve(), source_id=args.source_id)
    except RuntimeError as exc:
        print(f"link_ingest_error code={str(exc) or 'link_processing_failed'}")
        return 2
    except (OSError, ValueError, OverflowError):
        print("link_ingest_error code=unexpected_local_error")
        return 2

    print(
        "link_ingest_ok "
        f"scheme={result['scheme']} host_hash={result['host_hash']} "
        f"query={1 if result['has_query'] else 0} fragment={1 if result['has_fragment'] else 0}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())