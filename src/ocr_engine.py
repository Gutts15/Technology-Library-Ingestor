#!/usr/bin/env python3
"""Small Tesseract wrapper for selected Technology Library keyframes.

This module intentionally shells out to the system Tesseract executable instead
of adding a Python OCR dependency. It receives only already-selected keyframes.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_LANGUAGES = "eng+por"
DEFAULT_MAX_CHARS_PER_FRAME = 1500
WHITESPACE_RE = re.compile(r"[ \t]+")


def normalize_text(text: str, max_chars: int = DEFAULT_MAX_CHARS_PER_FRAME) -> str:
    lines: list[str] = []
    for raw_line in text.replace("\r", "\n").split("\n"):
        line = WHITESPACE_RE.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
    normalized = "\n".join(lines).strip()
    return normalized[:max_chars]


def installed_languages() -> set[str]:
    executable = shutil.which("tesseract")
    if executable is None:
        return set()
    result = subprocess.run(
        [executable, "--list-langs"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return set()
    languages = set()
    for line in result.stdout.splitlines():
        value = line.strip()
        if value and not value.lower().startswith("list of available languages"):
            languages.add(value)
    return languages


def resolve_languages(requested: str, available: set[str]) -> str | None:
    requested_parts = [part.strip() for part in requested.split("+") if part.strip()]
    usable = [part for part in requested_parts if part in available]
    if usable:
        return "+".join(usable)
    if "eng" in available:
        return "eng"
    if available:
        return sorted(available)[0]
    return None


def ocr_frame(frame_path: Path, languages: str) -> str | None:
    executable = shutil.which("tesseract")
    if executable is None:
        return None
    command = [
        executable,
        str(frame_path),
        "stdout",
        "-l",
        languages,
        "--psm",
        "6",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return normalize_text(result.stdout)


def build_ocr_artifact(
    out_dir: Path,
    keyframes: list[dict[str, Any]],
    requested_languages: str = DEFAULT_LANGUAGES,
) -> tuple[str | None, dict[str, str], dict[str, Any], list[str]]:
    """OCR selected keyframes and write a compact structured artifact."""
    warnings: list[str] = []
    available = installed_languages()
    languages = resolve_languages(requested_languages, available)
    if languages is None:
        warnings.append("ocr_unavailable")
        return None, {}, {
            "enabled": True,
            "languages": None,
            "frames_attempted": 0,
            "frames_with_text": 0,
        }, warnings

    records: list[dict[str, Any]] = []
    text_by_frame: dict[str, str] = {}
    attempted = 0
    with_text = 0

    for frame in keyframes:
        attempted += 1
        frame_rel = str(frame["path"])
        text = ocr_frame(out_dir / frame_rel, languages)
        if text is None:
            warnings.append("ocr_frame_failed")
            text = ""
        if text:
            with_text += 1
            text_by_frame[frame_rel] = text
        records.append(
            {
                "timestamp_seconds": frame["timestamp_seconds"],
                "frame": frame_rel,
                "text": text,
            }
        )

    artifact = {
        "schema_version": 1,
        "engine": "tesseract",
        "languages": languages,
        "records": records,
    }
    artifact_path = out_dir / "ocr.json"
    artifact_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    stats = {
        "enabled": True,
        "languages": languages,
        "frames_attempted": attempted,
        "frames_with_text": with_text,
    }
    return "ocr.json", text_by_frame, stats, sorted(set(warnings))
