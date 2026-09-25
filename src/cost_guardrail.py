#!/usr/bin/env python3
"""Fail closed when mandatory paid-model/API wiring appears in executable repo paths.

This guardrail intentionally scans only implementation/configuration paths, not
project documentation. Documentation may discuss paid APIs as rejected/optional
alternatives. The normal Technology Library runtime must not depend on them.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

GUARDRAIL_VERSION = "0.1.0"

DEFAULT_SCAN_PATHS = (
    Path("src"),
    Path(".github/workflows"),
)
ROOT_FILES = (
    Path("requirements.txt"),
    Path("requirements-transcription.txt"),
    Path("requirements-spreadsheet.txt"),
    Path("pyproject.toml"),
)
TEXT_SUFFIXES = {".py", ".yml", ".yaml", ".txt", ".toml", ".json", ".sh", ".ps1"}
EXCLUDED_FILES = {Path("src/cost_guardrail.py")}

# Presence in executable/config paths is enough to require explicit review.
FORBIDDEN_PATTERNS = {
    "openai_api_secret": re.compile(r"\bOPENAI_API_KEY\b", re.IGNORECASE),
    "anthropic_api_secret": re.compile(r"\bANTHROPIC_API_KEY\b", re.IGNORECASE),
    "gemini_api_secret": re.compile(r"\bGEMINI_API_KEY\b|\bGOOGLE_API_KEY\b", re.IGNORECASE),
    "cohere_api_secret": re.compile(r"\bCOHERE_API_KEY\b", re.IGNORECASE),
    "mistral_api_secret": re.compile(r"\bMISTRAL_API_KEY\b", re.IGNORECASE),
    "groq_api_secret": re.compile(r"\bGROQ_API_KEY\b", re.IGNORECASE),
    "together_api_secret": re.compile(r"\bTOGETHER_API_KEY\b", re.IGNORECASE),
}

# Paid model SDK packages are blocked as normal dependencies. HTTP libraries are
# not blocked because they are used for non-paid/provider-neutral operations.
FORBIDDEN_REQUIREMENTS = re.compile(
    r"^\s*(openai|anthropic|cohere|mistralai|groq|together)\s*(?:[<>=!~].*)?$",
    re.IGNORECASE,
)


def iter_files(root: Path) -> list[Path]:
    files: set[Path] = set()
    for relative in DEFAULT_SCAN_PATHS:
        base = root / relative
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
                files.add(path)
    for relative in ROOT_FILES:
        path = root / relative
        if path.is_file():
            files.add(path)
    return sorted(files)


def relative_to_root(path: Path, root: Path) -> Path:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return path


def scan_file(path: Path, root: Path) -> list[str]:
    relative = relative_to_root(path, root)
    if relative in EXCLUDED_FILES:
        return []

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ["unreadable_text_file"]

    findings: list[str] = []
    for code, pattern in FORBIDDEN_PATTERNS.items():
        if pattern.search(text):
            findings.append(code)

    if path.name.startswith("requirements"):
        for line in text.splitlines():
            stripped = line.split("#", 1)[0].strip()
            if stripped and FORBIDDEN_REQUIREMENTS.fullmatch(stripped):
                findings.append("paid_model_sdk_dependency")
                break

    return sorted(set(findings))


def main() -> int:
    parser = argparse.ArgumentParser(description="Enforce the zero-additional-cost runtime policy.")
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()

    root = args.root.resolve()
    files = iter_files(root)
    violations: list[tuple[Path, list[str]]] = []

    for path in files:
        findings = scan_file(path, root)
        if findings:
            violations.append((relative_to_root(path, root), findings))

    if violations:
        # Report only repository paths and stable violation codes, never secret values.
        for path, findings in violations:
            print(f"cost_guardrail_violation path={path.as_posix()} codes={','.join(findings)}")
        print(f"cost_guardrail_error version={GUARDRAIL_VERSION} violations={len(violations)}")
        return 2

    print(f"cost_guardrail_ok version={GUARDRAIL_VERSION} files={len(files)} violations=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
