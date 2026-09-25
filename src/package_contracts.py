#!/usr/bin/env python3
"""Minimum package contracts for safe reconciliation and retention.

A package is not considered complete merely because its generic manifest and
summary exist. Pipelines that depend on selective retrieval, visual evidence,
audible evidence or parsed link evidence must keep those required artifacts
before an original source can be finalized or become retention eligible.
"""

from __future__ import annotations

BASE_REQUIRED_FILES = (
    "ingest.json",
    "checkpoint.json",
    "evidence-summary.json",
    "process-record.json",
)

QUEUE_REQUIRED_FILES: dict[str, tuple[str, ...]] = {
    "99_INBOX/TO_REVIEW/VIDEOS": BASE_REQUIRED_FILES,
    "99_INBOX/TO_REVIEW/IMAGES": BASE_REQUIRED_FILES + ("image-index.json", "preview.jpg"),
    "99_INBOX/TO_REVIEW/AUDIO": BASE_REQUIRED_FILES + ("audio-index.json", "preview.opus"),
    "99_INBOX/TO_REVIEW/TEXT": BASE_REQUIRED_FILES + ("content-index.json",),
    "99_INBOX/TO_REVIEW/DOCUMENTS": BASE_REQUIRED_FILES + ("content-index.json",),
    "99_INBOX/TO_REVIEW/SPREADSHEETS": BASE_REQUIRED_FILES + ("spreadsheet-index.json",),
    "99_INBOX/TO_REVIEW/LINKS": BASE_REQUIRED_FILES + ("link-index.json",),
}


def normalize_folder(value: str | None) -> str:
    return value.strip("/") if isinstance(value, str) else ""


def required_package_files(source_folder: str | None) -> tuple[str, ...] | None:
    """Return the strict required files for a known source queue.

    Unknown queues return None rather than falling back to a weaker contract.
    Safety-sensitive callers should therefore preserve/guard the source instead
    of guessing what constitutes a complete package.
    """
    return QUEUE_REQUIRED_FILES.get(normalize_folder(source_folder))