#!/usr/bin/env python3
"""Provider-neutral operational records for Technology Library ingestion.

A process record is private operational metadata. It is deliberately separate
from the human-facing Google Sheet that existed during prototyping so storage
and reporting backends can change without changing the ingest pipeline.

The module has no third-party dependencies and never prints record contents.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROCESS_RECORD_SCHEMA_VERSION = 1
PROCESS_RECORD_VERSION = "0.1.0"
DEFAULT_RETENTION_DAYS = 30


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_received_at(item: dict[str, Any]) -> str | None:
    value = item.get("ModTime")
    return str(value) if value else None


def source_media_type(item: dict[str, Any]) -> str | None:
    value = item.get("MimeType")
    return str(value) if value else None


def build_process_record(
    *,
    provider: str,
    source_id: str,
    source_name: str,
    source_folder: str,
    package_id: str,
    pipeline_version: str,
    compactor_version: str,
    status: str,
    item: dict[str, Any] | None = None,
    destination_folder: str | None = None,
    error_code: str | None = None,
    retryable: bool = False,
    keep_original: bool = False,
    source_recoverable: bool | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> dict[str, Any]:
    metadata = item or {}
    return {
        "schema_version": PROCESS_RECORD_SCHEMA_VERSION,
        "record_version": PROCESS_RECORD_VERSION,
        "source": {
            "provider": provider,
            "file_id": source_id,
            "name": source_name,
            "media_type": source_media_type(metadata),
            "source_folder": source_folder,
            "received_at": source_received_at(metadata),
        },
        "package_id": package_id,
        "processing": {
            "status": status,
            "error_code": error_code,
            "retryable": bool(retryable),
            "processed_at": utc_now(),
            "pipeline_version": pipeline_version,
            "compactor_version": compactor_version,
        },
        "routing": {
            "destination_folder": destination_folder,
            "keep_original": bool(keep_original),
            "source_recoverable": source_recoverable,
            "retention_days": int(retention_days),
        },
    }


def write_process_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
