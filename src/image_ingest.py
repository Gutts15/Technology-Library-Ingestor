#!/usr/bin/env python3
"""Mechanical local image ingestion for Technology Library.

V1 supports static JPEG/JPG, PNG, WebP and BMP. It extracts bounded technical
metadata, creates a metadata-stripped JPEG preview, computes SHA-256 and a
64-bit perceptual dHash, and can run optional Tesseract OCR. It performs no
semantic image classification and uses no paid API or LLM.

Normal stdout contains only aggregate technical status, never private filenames
or OCR content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

IMAGE_PIPELINE_VERSION = "0.1.0"
IMAGE_SUMMARY_VERSION = "0.1.0"
SCHEMA_VERSION = 1
MAX_SUMMARY_BYTES = 3072
MAX_INPUT_BYTES = 100 * 1024 * 1024
MAX_PIXELS = 60_000_000
MAX_DIMENSION = 30_000
PREVIEW_BOUND = 1280
OCR_BOUND = 2400
MAX_OCR_CHARS = 20_000
MAX_OCR_SAMPLE_CHARS = 500
SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


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


def encoded_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def compact_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if encoded_size(payload) <= MAX_SUMMARY_BYTES:
        return payload

    evidence = payload.get("evidence")
    if isinstance(evidence, dict):
        sample = evidence.get("ocr_sample")
        if isinstance(sample, str):
            for limit in (250, 120, 60, 0):
                evidence["ocr_sample"] = sample[:limit]
                if encoded_size(payload) <= MAX_SUMMARY_BYTES:
                    return payload
        if encoded_size(payload) > MAX_SUMMARY_BYTES:
            payload.pop("evidence", None)

    if encoded_size(payload) > MAX_SUMMARY_BYTES:
        payload = {
            "schema_version": payload.get("schema_version"),
            "summary_version": payload.get("summary_version"),
            "source": payload.get("source"),
            "image": payload.get("image"),
            "audit": payload.get("audit"),
        }
    return payload


def run_text(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def probe_image(path: Path) -> dict[str, Any]:
    result = run_text([
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=codec_name,width,height,pix_fmt,color_space,color_range,color_transfer,color_primaries:format=format_name,size",
        "-of", "json",
        str(path),
    ])
    if result.returncode != 0:
        raise RuntimeError("image_probe_failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("image_probe_invalid_json") from exc

    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list) or not streams or not isinstance(streams[0], dict):
        raise RuntimeError("image_stream_missing")
    stream = streams[0]
    try:
        width = int(stream.get("width"))
        height = int(stream.get("height"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("image_dimensions_missing") from exc
    if width < 1 or height < 1:
        raise RuntimeError("image_dimensions_invalid")
    if width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
        raise RuntimeError("image_dimensions_exceeded")

    format_payload = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    return {
        "width": width,
        "height": height,
        "pixels": width * height,
        "codec": stream.get("codec_name"),
        "pixel_format": stream.get("pix_fmt"),
        "color_space": stream.get("color_space"),
        "color_range": stream.get("color_range"),
        "color_transfer": stream.get("color_transfer"),
        "color_primaries": stream.get("color_primaries"),
        "container_format": format_payload.get("format_name"),
    }


def generate_scaled_image(source: Path, destination: Path, bound: int, *, png: bool) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(source),
        "-map_metadata", "-1",
        "-frames:v", "1",
        "-vf", f"scale={bound}:{bound}:force_original_aspect_ratio=decrease",
    ]
    if png:
        command += ["-compression_level", "6"]
    else:
        command += ["-q:v", "3"]
    command += ["-y", str(destination)]
    result = run_text(command)
    if result.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError("image_normalization_failed")


def dhash64(path: Path) -> str:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(path),
            "-frames:v", "1",
            "-vf", "scale=9:8,format=gray",
            "-f", "rawvideo",
            "-pix_fmt", "gray",
            "pipe:1",
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0 or len(result.stdout) < 72:
        raise RuntimeError("image_fingerprint_failed")
    pixels = result.stdout[:72]
    value = 0
    bit = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            if pixels[offset + column] > pixels[offset + column + 1]:
                value |= 1 << bit
            bit += 1
    return f"{value:016x}"


def normalize_ocr_text(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.replace("\r", "\n").split("\n")]
    return "\n".join(line for line in lines if line).strip()


def run_ocr(source: Path, out: Path, languages: str) -> dict[str, Any]:
    temp = out / ".ocr-input.png"
    try:
        generate_scaled_image(source, temp, OCR_BOUND, png=True)
        result = run_text([
            "tesseract",
            str(temp),
            "stdout",
            "-l", languages,
            "--psm", "11",
        ])
        if result.returncode != 0:
            raise RuntimeError("ocr_failed")
        text = normalize_ocr_text(result.stdout)
        truncated = len(text) > MAX_OCR_CHARS
        bounded = text[:MAX_OCR_CHARS]
        payload = {
            "schema_version": 1,
            "engine": "tesseract",
            "languages": languages,
            "text": bounded,
            "characters": len(bounded),
            "truncated": truncated,
        }
        write_json(out / "ocr.json", payload)
        return payload
    finally:
        temp.unlink(missing_ok=True)


def build_summary(
    *,
    source_id: str | None,
    source_sha: str,
    metadata: dict[str, Any],
    fingerprint: str,
    ocr: dict[str, Any] | None,
) -> dict[str, Any]:
    ocr_text = str(ocr.get("text") or "") if isinstance(ocr, dict) else ""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "summary_version": IMAGE_SUMMARY_VERSION,
        "source": {
            "provider": "external",
            "file_id": source_id,
            "sha256": source_sha,
        },
        "image": {
            "width": metadata["width"],
            "height": metadata["height"],
            "codec": metadata.get("codec"),
            "pixel_format": metadata.get("pixel_format"),
            "dhash64": fingerprint,
            "ocr": {
                "performed": ocr is not None,
                "characters": int(ocr.get("characters") or 0) if isinstance(ocr, dict) else 0,
                "truncated": bool(ocr.get("truncated")) if isinstance(ocr, dict) else False,
            },
        },
        "evidence": {
            "ocr_sample": ocr_text[:MAX_OCR_SAMPLE_CHARS],
        },
        "audit": {
            "manifest": "ingest.json",
            "index": "image-index.json",
            "preview": "preview.jpg",
            "ocr": "ocr.json" if ocr is not None else None,
        },
    }
    return compact_summary(payload)


def ingest_image(
    source: Path,
    out: Path,
    *,
    source_id: str | None,
    ocr_enabled: bool,
    ocr_languages: str,
) -> dict[str, Any]:
    if not source.is_file():
        raise RuntimeError("source_missing")
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise RuntimeError("unsupported_image_format")
    size = source.stat().st_size
    if size < 1:
        raise RuntimeError("source_empty")
    if size > MAX_INPUT_BYTES:
        raise RuntimeError("source_too_large")

    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg_missing")
    if ocr_enabled and not shutil.which("tesseract"):
        raise RuntimeError("tesseract_missing")

    out.mkdir(parents=True, exist_ok=True)
    source_sha = sha256_file(source)
    metadata = probe_image(source)
    fingerprint = dhash64(source)

    preview = out / "preview.jpg"
    generate_scaled_image(source, preview, PREVIEW_BOUND, png=False)

    ocr_payload = run_ocr(source, out, ocr_languages) if ocr_enabled else None

    image_index = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": IMAGE_PIPELINE_VERSION,
        "source": {
            "sha256": source_sha,
            "bytes": size,
        },
        "image": metadata,
        "fingerprint": {
            "algorithm": "dhash64",
            "value": fingerprint,
        },
        "artifacts": {
            "preview": "preview.jpg",
            "preview_sha256": sha256_file(preview),
            "ocr": "ocr.json" if ocr_payload is not None else None,
        },
        "privacy": {
            "embedded_metadata_exported": False,
            "preview_metadata_stripped": True,
        },
    }
    write_json(out / "image-index.json", image_index)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": IMAGE_PIPELINE_VERSION,
        "created_at": utc_now(),
        "source": {
            "provider": "external",
            "file_id": source_id,
            "sha256": source_sha,
            "bytes": size,
        },
        "image": metadata,
        "fingerprint": {"algorithm": "dhash64", "value": fingerprint},
        "artifacts": {
            "index": "image-index.json",
            "preview": "preview.jpg",
            "ocr": "ocr.json" if ocr_payload is not None else None,
        },
    }
    write_json(out / "ingest.json", manifest)

    summary = build_summary(
        source_id=source_id,
        source_sha=source_sha,
        metadata=metadata,
        fingerprint=fingerprint,
        ocr=ocr_payload,
    )
    write_json(out / "evidence-summary.json", summary, compact=True)
    if (out / "evidence-summary.json").stat().st_size > MAX_SUMMARY_BYTES:
        raise RuntimeError("summary_budget_exceeded")

    checkpoint = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": IMAGE_PIPELINE_VERSION,
        "summary_version": IMAGE_SUMMARY_VERSION,
        "source_sha256": source_sha,
        "status": "complete",
        "required_artifacts": [
            "ingest.json",
            "checkpoint.json",
            "image-index.json",
            "evidence-summary.json",
            "preview.jpg",
        ],
    }
    write_json(out / "checkpoint.json", checkpoint)

    return {
        "pipeline_version": IMAGE_PIPELINE_VERSION,
        "summary_version": IMAGE_SUMMARY_VERSION,
        "width": metadata["width"],
        "height": metadata["height"],
        "ocr_performed": ocr_payload is not None,
        "ocr_characters": int(ocr_payload.get("characters") or 0) if ocr_payload else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create compact mechanical evidence from a static image.")
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-id")
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--ocr-languages", default="eng+por")
    args = parser.parse_args()

    try:
        result = ingest_image(
            args.source.resolve(),
            args.out.resolve(),
            source_id=args.source_id,
            ocr_enabled=args.ocr,
            ocr_languages=args.ocr_languages,
        )
    except RuntimeError as exc:
        code = str(exc) or "image_processing_failed"
        print(f"image_ingest_error code={code}")
        return 2
    except (OSError, ValueError, OverflowError):
        print("image_ingest_error code=unexpected_local_error")
        return 2

    print(
        "image_ingest_ok "
        f"width={result['width']} height={result['height']} "
        f"ocr={1 if result['ocr_performed'] else 0} ocr_chars={result['ocr_characters']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
