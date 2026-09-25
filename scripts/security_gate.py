#!/usr/bin/env python3
"""Small, dependency-free security gate for Technology Library Ingestor.

This is deliberately dependency-free. It protects the project's high-value
repository privacy invariants without turning the project into a security product.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_SUFFIXES = {
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic",
    ".wav", ".mp3", ".m4a", ".aac", ".flac",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm",
    ".ppt", ".pptx", ".odt", ".ods",
    ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz",
    ".csv", ".tsv", ".sqlite", ".sqlite3", ".db",
    ".env", ".pem", ".key", ".p12", ".pfx",
}

FORBIDDEN_PATH_PARTS = {
    "input", "inputs", "inbox", "raw", "downloads", "work", "tmp", "cache",
    "ready_for_analysis", "frame", "frames", "transcript", "transcripts",
    "ocr", "output", "outputs", "private",
}

FORBIDDEN_FILENAME_PATTERNS = {
    "credentials": re.compile(r"^credentials(?:[-_.].*)?\.json$", re.IGNORECASE),
    "client_secret": re.compile(r"^client_secret(?:[-_.].*)?\.json$", re.IGNORECASE),
    "token": re.compile(r"^token(?:[-_.].*)?\.json$", re.IGNORECASE),
    "cookies": re.compile(r"^cookies(?:[-_.].*)?\.txt$", re.IGNORECASE),
    "service_account": re.compile(r"^service[-_]account(?:[-_.].*)?\.json$", re.IGNORECASE),
    "rclone_config": re.compile(r"^(?:.*\.)?rclone\.conf$", re.IGNORECASE),
    "environment": re.compile(r"^\.env(?:\..+)?$", re.IGNORECASE),
    "private_derivative": re.compile(
        r"^(?:transcript|ocr|contact[-_]?sheet)(?:[-_.].*)?\.(?:txt|md|json|csv|tsv|png|jpe?g)$",
        re.IGNORECASE,
    ),
}

ALLOWED_EXAMPLE_NAMES = {".env.example"}

PRIVATE_ONLY_PATHS = {
    "curation/decisions.json",
    "docs/current_state.md",
    "docs/next_local_checkpoint.md",
    "docs/library_retrieval_results.md",
    "docs/library_retrieval_validation.md",
    "docs/chatgpt_global_tech_library.md",
    "docs/chatgpt_project_tech_routing.md",
    "docs/alignment_incident_2026-09-23.md",
    "docs/v1_core_roadmap_history.md",
    "docs/memory_vault_handoff.md",
}

HIGH_SIGNAL_PRIVATE_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_pat": re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    "google_oauth_refresh_token": re.compile(r"1//[0-9A-Za-z_-]{20,}"),
    "openai_key": re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "aws_access_key": re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    "slack_token": re.compile(r"xox[baprs]-[0-9A-Za-z-]{20,}"),
    "stripe_live_key": re.compile(r"[rs]k_live_[0-9A-Za-z]{20,}"),
    "private_google_drive_url": re.compile(r"https?://(?:drive|docs)\.google\.com/", re.IGNORECASE),
}

PYTHON_DANGEROUS_PATTERNS = {
    "eval": re.compile(r"\beval\s*\("),
    "exec": re.compile(r"\bexec\s*\("),
    "os_system": re.compile(r"\bos\.system\s*\("),
    "shell_true": re.compile(r"shell\s*=\s*True"),
}


def tracked_files() -> list[Path]:
    if not (ROOT / ".git").exists():
        return sorted(path for path in ROOT.rglob("*") if path.is_file())
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    )
    return [ROOT / item.decode() for item in result.stdout.split(b"\0") if item]


def text_of(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def forbidden_path_reason(rel: Path) -> str | None:
    parts = tuple(part.casefold() for part in rel.parts)
    name = rel.name.casefold()
    if rel.as_posix().casefold() in PRIVATE_ONLY_PATHS:
        return "private-only path"
    if parts and parts[0] == "curation":
        return "live private curation state"
    if any(part in FORBIDDEN_PATH_PARTS for part in parts):
        return "generated/private path"
    if rel.suffix.casefold() in FORBIDDEN_SUFFIXES:
        return "file type"
    if name not in ALLOWED_EXAMPLE_NAMES:
        for family, pattern in FORBIDDEN_FILENAME_PATTERNS.items():
            if pattern.fullmatch(rel.name):
                return f"{family} filename"
    return None


def check_tracked_files(files: list[Path], errors: list[str]) -> None:
    for path in files:
        rel = path.relative_to(ROOT)
        reason = forbidden_path_reason(rel)
        if reason:
            fail(errors, f"forbidden tracked {reason}: {rel}")


def check_secret_patterns(files: list[Path], errors: list[str]) -> None:
    for path in files:
        text = text_of(path)
        if text is None:
            fail(errors, f"tracked file is not readable UTF-8 text: {path.relative_to(ROOT)}")
            continue
        for name, pattern in HIGH_SIGNAL_PRIVATE_PATTERNS.items():
            if pattern.search(text):
                fail(errors, f"possible private value {name} found in tracked file: {path.relative_to(ROOT)}")


def check_python(files: list[Path], errors: list[str]) -> None:
    for path in files:
        if path.suffix != ".py":
            continue
        text = text_of(path) or ""
        for name, pattern in PYTHON_DANGEROUS_PATTERNS.items():
            if pattern.search(text):
                fail(errors, f"dangerous Python pattern {name}: {path.relative_to(ROOT)}")


def check_workflows(files: list[Path], errors: list[str]) -> None:
    workflows = [p for p in files if p.relative_to(ROOT).parts[:2] == (".github", "workflows")]
    sha_pin = re.compile(r"^\s*uses:\s*[^\s@]+@([0-9a-fA-F]{40})(?:\s*#.*)?$", re.MULTILINE)
    any_uses = re.compile(r"^\s*uses:\s*([^\s]+)", re.MULTILINE)

    for path in workflows:
        text = text_of(path) or ""
        rel = path.relative_to(ROOT)
        is_keepalive = rel.as_posix() == ".github/workflows/scheduler-keepalive.yml"

        if "permissions:" not in text:
            fail(errors, f"workflow missing explicit permissions: {rel}")
        for forbidden in ("write-all", "contents: write", "actions: write", "id-token: write", "pull_request_target"):
            if forbidden == "contents: write" and is_keepalive:
                continue
            if forbidden in text:
                fail(errors, f"forbidden workflow capability '{forbidden}': {rel}")
        if is_keepalive:
            if "contents: write" not in text or "  schedule:" not in text:
                fail(errors, "keepalive requires a schedule and narrowly scoped write permission")
            if ("secrets." in text or "${{ secrets" in text or
                    re.search(r"(?m)^\s{2}(?:pull_request|workflow_dispatch|push)\s*:", text)):
                fail(errors, "keepalive must be scheduled only and receive no repository secrets")
        if "actions/upload-artifact" in text:
            fail(errors, f"artifact upload is forbidden in V1 workflows: {rel}")

        pr_trigger = bool(
            re.search(r"(?m)^\s{2}pull_request\s*:", text)
            or re.search(r"(?m)^on:\s*\[[^\]]*\bpull_request\b", text)
        )
        if pr_trigger and (
            "secrets." in text
            or "${{ secrets" in text
            or re.search(r"(?m)^\s*secrets:\s*inherit\s*$", text)
        ):
            fail(errors, f"pull_request workflow must not receive repository secrets: {rel}")

        for match in any_uses.finditer(text):
            line = match.group(0)
            if not sha_pin.match(line):
                fail(errors, f"third-party action is not pinned to an immutable 40-char SHA: {rel}: {line.strip()}")

    security_workflow = ROOT / ".github/workflows/security-gate.yml"
    if security_workflow.exists():
        text = text_of(security_workflow) or ""
        if "secrets." in text or "${{ secrets" in text:
            fail(errors, "security-gate.yml must not receive repository secrets")


def main() -> int:
    errors: list[str] = []
    files = tracked_files()

    check_tracked_files(files, errors)
    check_secret_patterns(files, errors)
    check_python(files, errors)
    check_workflows(files, errors)

    if errors:
        print("security_gate_failed")
        for item in errors:
            print(f"- {item}")
        return 1

    print(f"security_gate_ok tracked_files={len(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
