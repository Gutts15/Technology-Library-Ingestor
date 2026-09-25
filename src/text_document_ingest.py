#!/usr/bin/env python3
"""Local text/document ingestion for Technology Library.

Supported V1 inputs:
- plain text and source-code files
- PDF via the local pdftotext executable
- DOCX via Python stdlib zip/XML parsing
- PPTX via Python stdlib zip/XML parsing

The processor performs no semantic classification and calls no external API.
It writes selective-retrieval chunks plus a compact <=3 KB evidence summary.
Normal stdout never contains private filenames or extracted content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

CONTENT_PIPELINE_VERSION = "0.1.0"
CONTENT_SUMMARY_VERSION = "0.1.0"
SCHEMA_VERSION = 1
MAX_SUMMARY_BYTES = 3072
DEFAULT_CHUNK_CHARS = 48000
MAX_INPUT_BYTES = 100 * 1024 * 1024

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".yaml", ".yml", ".xml",
    ".html", ".htm", ".log", ".ini", ".cfg", ".conf", ".toml", ".env",
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".css",
    ".scss", ".sass", ".less", ".sql", ".sh", ".bash", ".zsh", ".ps1",
    ".bat", ".cmd", ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".java",
    ".kt", ".kts", ".go", ".rs", ".rb", ".php", ".swift", ".dart", ".lua",
    ".r", ".scala", ".vue", ".svelte", ".graphql", ".gql", ".proto",
}
DOCUMENT_SUFFIXES = {".pdf", ".docx", ".pptx"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def decode_text_bytes(data: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1-fallback"


def normalize_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{5,}", "\n\n\n\n", text)
    return text.strip() + ("\n" if text.strip() else "")


def extract_plain_text(path: Path) -> tuple[str, dict[str, Any]]:
    text, encoding = decode_text_bytes(path.read_bytes())
    return normalize_text(text), {"method": "decode", "encoding": encoding}


def extract_pdf(path: Path) -> tuple[str, dict[str, Any]]:
    if not shutil.which("pdftotext"):
        raise RuntimeError("pdftotext_missing")
    result = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError("pdf_extract_failed")
    text, encoding = decode_text_bytes(result.stdout)
    page_count = max(1, text.count("\f") + 1) if text.strip() else 0
    text = text.replace("\f", "\n\n--- PAGE BREAK ---\n\n")
    return normalize_text(text), {
        "method": "pdftotext-layout",
        "encoding": encoding,
        "pages_estimate": page_count,
    }


def extract_docx(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise RuntimeError("docx_extract_failed") from exc

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise RuntimeError("docx_extract_failed") from exc

    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    for paragraph in root.iter(ns + "p"):
        pieces = [(node.text or "") for node in paragraph.iter(ns + "t")]
        line = "".join(pieces).strip()
        if line:
            paragraphs.append(line)
    return normalize_text("\n".join(paragraphs)), {
        "method": "docx-stdlib-xml",
        "paragraphs": len(paragraphs),
    }


def slide_number(name: str) -> int:
    match = re.search(r"slide(\d+)\.xml$", name)
    return int(match.group(1)) if match else 10**9


def extract_pptx(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        with zipfile.ZipFile(path) as archive:
            slide_names = sorted(
                [name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)],
                key=slide_number,
            )
            slides: list[str] = []
            for index, name in enumerate(slide_names, start=1):
                try:
                    root = ET.fromstring(archive.read(name))
                except ET.ParseError:
                    continue
                texts = [
                    (node.text or "").strip()
                    for node in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t")
                    if (node.text or "").strip()
                ]
                slides.append(f"## Slide {index}\n" + "\n".join(texts))
    except zipfile.BadZipFile as exc:
        raise RuntimeError("pptx_extract_failed") from exc

    return normalize_text("\n\n".join(slides)), {
        "method": "pptx-stdlib-xml",
        "slides": len(slide_names) if 'slide_names' in locals() else 0,
    }


def extract_content(path: Path) -> tuple[str, str, dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        text, metadata = extract_plain_text(path)
        return text, "text", metadata
    if suffix == ".pdf":
        text, metadata = extract_pdf(path)
        return text, "pdf", metadata
    if suffix == ".docx":
        text, metadata = extract_docx(path)
        return text, "docx", metadata
    if suffix == ".pptx":
        text, metadata = extract_pptx(path)
        return text, "pptx", metadata
    raise RuntimeError("unsupported_format")


def split_chunks(text: str, max_chars: int) -> list[str]:
    if not text:
        return []
    paragraphs = re.split(r"(?<=\n)\n+", text)
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        paragraph = paragraph.strip("\n")
        if not paragraph:
            continue
        candidate = paragraph if not current else current + "\n\n" + paragraph
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current + "\n")
            current = ""
        if len(paragraph) <= max_chars:
            current = paragraph
            continue
        for start in range(0, len(paragraph), max_chars):
            piece = paragraph[start:start + max_chars]
            if len(piece) == max_chars:
                chunks.append(piece + "\n")
            else:
                current = piece
    if current:
        chunks.append(current + "\n")
    return chunks


def sample_text(text: str, start: int, length: int = 420) -> str:
    if not text:
        return ""
    start = max(0, min(start, max(0, len(text) - 1)))
    sample = text[start:start + length]
    sample = re.sub(r"\s+", " ", sample).strip()
    return sample


def compact_summary(payload: dict[str, Any]) -> dict[str, Any]:
    def encoded_size(value: dict[str, Any]) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    if encoded_size(payload) <= MAX_SUMMARY_BYTES:
        return payload

    evidence = payload.get("evidence")
    if isinstance(evidence, dict):
        samples = evidence.get("samples")
        if isinstance(samples, list):
            for limit in (300, 220, 140, 80):
                evidence["samples"] = [str(item)[:limit] for item in samples]
                if encoded_size(payload) <= MAX_SUMMARY_BYTES:
                    return payload
            evidence["samples"] = evidence["samples"][:1]

    if encoded_size(payload) > MAX_SUMMARY_BYTES:
        payload.pop("extraction", None)
    if encoded_size(payload) > MAX_SUMMARY_BYTES:
        payload["evidence"] = {"samples": []}
    if encoded_size(payload) > MAX_SUMMARY_BYTES:
        payload = {
            "schema_version": payload.get("schema_version"),
            "summary_version": payload.get("summary_version"),
            "source": payload.get("source"),
            "content": payload.get("content"),
            "audit": payload.get("audit"),
        }
    return payload


def build_summary(
    *,
    source_id: str | None,
    source_sha: str,
    content_type: str,
    text: str,
    chunks: list[str],
    extraction: dict[str, Any],
) -> dict[str, Any]:
    positions = [0]
    if len(text) > 1000:
        positions.append(max(0, len(text) // 2 - 200))
    if len(text) > 1800:
        positions.append(max(0, len(text) - 500))
    samples = [sample_text(text, pos) for pos in positions]
    samples = [sample for sample in samples if sample]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "summary_version": CONTENT_SUMMARY_VERSION,
        "source": {
            "provider": "external",
            "file_id": source_id,
            "sha256": source_sha,
        },
        "content": {
            "type": content_type,
            "characters": len(text),
            "words": len(re.findall(r"\S+", text)),
            "lines": text.count("\n"),
            "chunks": len(chunks),
        },
        "extraction": extraction,
        "evidence": {"samples": samples},
        "audit": {
            "manifest": "ingest.json",
            "index": "content-index.json",
            "chunks": "chunks/",
        },
    }
    return compact_summary(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest text/PDF/DOCX/PPTX into compact private evidence.")
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS)
    args = parser.parse_args()

    source = args.source.resolve()
    out = args.out.resolve()
    if not source.is_file():
        print("content_ingest_error code=source_missing")
        return 2
    if source.stat().st_size > MAX_INPUT_BYTES:
        print("content_ingest_error code=input_too_large")
        return 2
    if not 4096 <= args.chunk_chars <= 200000:
        parser.error("--chunk-chars must be between 4096 and 200000")

    source_sha = sha256_file(source)
    try:
        text, content_type, extraction = extract_content(source)
    except RuntimeError as exc:
        print(f"content_ingest_error code={exc}")
        return 3

    chunks = split_chunks(text, args.chunk_chars)
    out.mkdir(parents=True, exist_ok=True)
    chunks_dir = out / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    index_chunks: list[dict[str, Any]] = []
    offset = 0
    for index, chunk in enumerate(chunks, start=1):
        name = f"chunk-{index:04d}.txt"
        path = chunks_dir / name
        path.write_text(chunk, encoding="utf-8")
        index_chunks.append({
            "index": index,
            "path": f"chunks/{name}",
            "characters": len(chunk),
            "start_character": offset,
            "end_character": offset + len(chunk),
        })
        offset += len(chunk)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": CONTENT_PIPELINE_VERSION,
        "processing": {"status": "processed", "processed_at": utc_now()},
        "source": {
            "provider": "external",
            "file_id": args.source_id,
            "sha256": source_sha,
            "size_bytes": source.stat().st_size,
        },
        "content": {
            "type": content_type,
            "characters": len(text),
            "words": len(re.findall(r"\S+", text)),
            "lines": text.count("\n"),
            "chunks": len(chunks),
        },
        "extraction": extraction,
    }
    write_json(out / "ingest.json", manifest)
    write_json(out / "content-index.json", {
        "schema_version": SCHEMA_VERSION,
        "content_type": content_type,
        "characters": len(text),
        "chunk_chars_target": args.chunk_chars,
        "chunks": index_chunks,
    })
    summary = build_summary(
        source_id=args.source_id,
        source_sha=source_sha,
        content_type=content_type,
        text=text,
        chunks=chunks,
        extraction=extraction,
    )
    write_json(out / "evidence-summary.json", summary)
    write_json(out / "checkpoint.json", {
        "schema_version": SCHEMA_VERSION,
        "status": "processed",
        "source_sha256": source_sha,
        "pipeline_version": CONTENT_PIPELINE_VERSION,
        "summary_version": CONTENT_SUMMARY_VERSION,
        "updated_at": utc_now(),
    })

    if (out / "evidence-summary.json").stat().st_size > MAX_SUMMARY_BYTES:
        print("content_ingest_error code=summary_budget_exceeded")
        return 4

    print(
        "content_ingest_ok "
        f"type={content_type} chunks={len(chunks)} chars={len(text)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
