#!/usr/bin/env python3
"""Technology Library video ingestor.

V0.5 keeps preprocessing local and compact:
- SHA-256 source hashing
- ffprobe metadata extraction
- scene-aware candidate timestamps
- uniform fallback sampling
- dHash perceptual deduplication without third-party Python packages
- compact JPEG keyframe extraction
- compact contact sheet generation
- optional Tesseract OCR on selected keyframes
- optional faster-whisper CPU transcription
- interleaved timeline.md evidence index
- ingest.json generation

No cloud credentials or persistence are used yet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ocr_engine import DEFAULT_LANGUAGES, build_ocr_artifact
from transcription_engine import DEFAULT_MODEL, build_transcript_artifact

PIPELINE_VERSION = "0.5.0"
SCHEMA_VERSION = 1
DEFAULT_MAX_KEYFRAMES = 12
DEFAULT_SCENE_THRESHOLD = 0.30
DEFAULT_DEDUPE_DISTANCE = 6
MAX_SCENE_CANDIDATES = 60
MAX_HASH_CANDIDATES = 48
CONTACT_COLUMNS = 4
CONTACT_THUMB_WIDTH = 320
CONTACT_THUMB_HEIGHT = 180
CONTACT_PADDING = 6

PTS_TIME_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def ffprobe(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=index,codec_type,width,height",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def video_metadata(probe: dict[str, Any]) -> dict[str, Any]:
    streams = probe.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    raw_duration = probe.get("format", {}).get("duration")
    duration = round(float(raw_duration), 3) if raw_duration is not None else None

    return {
        "duration_seconds": duration,
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "has_audio": has_audio,
    }


def detect_scene_timestamps(path: Path, threshold: float) -> list[float]:
    filter_graph = f"select=gt(scene\\,{threshold}),showinfo"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "info",
        "-i",
        str(path),
        "-an",
        "-vf",
        filter_graph,
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return []

    timestamps: list[float] = []
    for match in PTS_TIME_RE.finditer(result.stderr):
        timestamp = float(match.group(1))
        if not timestamps or abs(timestamp - timestamps[-1]) >= 0.10:
            timestamps.append(timestamp)
        if len(timestamps) >= MAX_SCENE_CANDIDATES:
            break
    return timestamps


def uniform_timestamps(duration: float, target_count: int) -> list[float]:
    if duration <= 0 or target_count <= 0:
        return [0.0]
    count = max(1, target_count)
    step = duration / count
    return [round(min(duration - 0.001, i * step), 3) for i in range(count)]


def merge_timestamps(
    duration: float,
    scene_timestamps: list[float],
    target_uniform: int,
) -> list[float]:
    candidates = [0.0, *scene_timestamps, *uniform_timestamps(duration, target_uniform)]
    cleaned: list[float] = []
    upper = max(0.0, duration - 0.001)
    for value in sorted(max(0.0, min(upper, ts)) for ts in candidates):
        if not cleaned or abs(value - cleaned[-1]) >= 0.12:
            cleaned.append(round(value, 3))

    if len(cleaned) <= MAX_HASH_CANDIDATES:
        return cleaned

    last = len(cleaned) - 1
    indexes = {
        round(i * last / (MAX_HASH_CANDIDATES - 1))
        for i in range(MAX_HASH_CANDIDATES)
    }
    return [cleaned[i] for i in sorted(indexes)]


def dhash_frame(path: Path, timestamp: float) -> int | None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-vf",
        "scale=9:8,format=gray",
        "-f",
        "rawvideo",
        "-",
    ]
    result = subprocess.run(command, check=False, capture_output=True)
    pixels = result.stdout
    if result.returncode != 0 or len(pixels) < 72:
        return None

    value = 0
    bit = 0
    for row in range(8):
        offset = row * 9
        for col in range(8):
            if pixels[offset + col] > pixels[offset + col + 1]:
                value |= 1 << bit
            bit += 1
    return value


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def unique_candidates(
    path: Path,
    timestamps: list[float],
    dedupe_distance: int,
) -> list[tuple[float, int]]:
    unique: list[tuple[float, int]] = []
    hashes: list[int] = []

    for timestamp in timestamps:
        frame_hash = dhash_frame(path, timestamp)
        if frame_hash is None:
            continue
        if any(hamming_distance(frame_hash, existing) <= dedupe_distance for existing in hashes):
            continue
        unique.append((timestamp, frame_hash))
        hashes.append(frame_hash)

    return unique


def spread_selection(
    candidates: list[tuple[float, int]],
    max_keyframes: int,
) -> list[tuple[float, int]]:
    if len(candidates) <= max_keyframes:
        return candidates
    if max_keyframes <= 1:
        return [candidates[0]]

    last = len(candidates) - 1
    indexes = {
        round(i * last / (max_keyframes - 1))
        for i in range(max_keyframes)
    }
    return [candidates[i] for i in sorted(indexes)]


def extract_jpeg(path: Path, timestamp: float, destination: Path) -> bool:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-vf",
        "scale=960:-2",
        "-q:v",
        "3",
        str(destination),
    ]
    result = subprocess.run(command, check=False, capture_output=True)
    return result.returncode == 0 and destination.is_file() and destination.stat().st_size > 0


def build_keyframes(
    source: Path,
    out_dir: Path,
    duration: float,
    max_keyframes: int,
    scene_threshold: float,
    dedupe_distance: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    scene_timestamps = detect_scene_timestamps(source, scene_threshold)
    timestamps = merge_timestamps(
        duration,
        scene_timestamps,
        target_uniform=max(max_keyframes * 2, 8),
    )
    unique = unique_candidates(source, timestamps, dedupe_distance)
    selected = spread_selection(unique, max_keyframes)

    keyframe_dir = out_dir / "keyframes"
    keyframe_dir.mkdir(parents=True, exist_ok=True)

    artifacts: list[dict[str, Any]] = []
    for index, (timestamp, frame_hash) in enumerate(selected, start=1):
        filename = f"frame_{index:03d}.jpg"
        destination = keyframe_dir / filename
        if not extract_jpeg(source, timestamp, destination):
            continue
        artifacts.append(
            {
                "timestamp_seconds": round(timestamp, 3),
                "path": f"keyframes/{filename}",
                "dhash": f"{frame_hash:016x}",
            }
        )

    stats = {
        "scene_candidates": len(scene_timestamps),
        "hash_candidates": len(timestamps),
        "unique_candidates": len(unique),
        "selected_keyframes": len(artifacts),
        "duplicates_removed": max(0, len(timestamps) - len(unique)),
        "scene_threshold": scene_threshold,
        "dedupe_distance": dedupe_distance,
    }
    return artifacts, stats


def raw_thumbnail(frame_path: Path) -> bytes | None:
    filter_graph = (
        f"scale={CONTACT_THUMB_WIDTH}:{CONTACT_THUMB_HEIGHT}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={CONTACT_THUMB_WIDTH}:{CONTACT_THUMB_HEIGHT}:"
        "(ow-iw)/2:(oh-ih)/2:color=black,format=rgb24"
    )
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(frame_path),
        "-frames:v",
        "1",
        "-vf",
        filter_graph,
        "-f",
        "rawvideo",
        "-",
    ]
    result = subprocess.run(command, check=False, capture_output=True)
    expected = CONTACT_THUMB_WIDTH * CONTACT_THUMB_HEIGHT * 3
    if result.returncode != 0 or len(result.stdout) != expected:
        return None
    return result.stdout


def build_contact_sheet(out_dir: Path, keyframes: list[dict[str, Any]]) -> str | None:
    if not keyframes:
        return None

    columns = min(CONTACT_COLUMNS, len(keyframes))
    rows = math.ceil(len(keyframes) / columns)
    width = columns * CONTACT_THUMB_WIDTH + (columns + 1) * CONTACT_PADDING
    height = rows * CONTACT_THUMB_HEIGHT + (rows + 1) * CONTACT_PADDING
    canvas = bytearray(width * height * 3)

    for index, frame in enumerate(keyframes):
        frame_path = out_dir / frame["path"]
        thumb = raw_thumbnail(frame_path)
        if thumb is None:
            return None

        column = index % columns
        row = index // columns
        x = CONTACT_PADDING + column * (CONTACT_THUMB_WIDTH + CONTACT_PADDING)
        y = CONTACT_PADDING + row * (CONTACT_THUMB_HEIGHT + CONTACT_PADDING)

        source_stride = CONTACT_THUMB_WIDTH * 3
        destination_stride = width * 3
        for thumb_row in range(CONTACT_THUMB_HEIGHT):
            src_start = thumb_row * source_stride
            src_end = src_start + source_stride
            dst_start = (y + thumb_row) * destination_stride + x * 3
            canvas[dst_start : dst_start + source_stride] = thumb[src_start:src_end]

    ppm_path = out_dir / ".contact-sheet.ppm"
    jpeg_path = out_dir / "contact-sheet.jpg"
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    ppm_path.write_bytes(header + canvas)

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(ppm_path),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(jpeg_path),
    ]
    result = subprocess.run(command, check=False, capture_output=True)
    ppm_path.unlink(missing_ok=True)

    if result.returncode != 0 or not jpeg_path.is_file() or jpeg_path.stat().st_size == 0:
        jpeg_path.unlink(missing_ok=True)
        return None
    return "contact-sheet.jpg"


def format_timestamp(seconds: float) -> str:
    milliseconds = int(round(max(0.0, seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def write_timeline(
    out_dir: Path,
    media: dict[str, Any],
    keyframes: list[dict[str, Any]],
    contact_sheet: str | None,
    ocr_by_frame: dict[str, str],
    transcript_segments: list[dict[str, Any]],
) -> str:
    """Write one compact chronological evidence stream without source name."""
    lines = [
        "# Video Evidence Timeline",
        "",
        f"- Duration: {format_timestamp(float(media.get('duration_seconds') or 0.0))}",
        f"- Keyframes: {len(keyframes)}",
        f"- Speech segments: {len(transcript_segments)}",
    ]
    if contact_sheet:
        lines.append(f"- Contact sheet: `{contact_sheet}`")
    lines.append("")

    events: list[tuple[float, int, str, dict[str, Any]]] = []
    for frame in keyframes:
        events.append((float(frame["timestamp_seconds"]), 0, "frame", frame))
    for segment in transcript_segments:
        events.append((float(segment["start_seconds"]), 1, "speech", segment))

    for _, _, event_type, payload in sorted(events, key=lambda item: (item[0], item[1])):
        if event_type == "frame":
            timestamp = format_timestamp(float(payload["timestamp_seconds"]))
            frame_path = str(payload["path"])
            lines.extend([f"## {timestamp} - Frame", "", f"- Frame: `{frame_path}`", ""])
            ocr_text = ocr_by_frame.get(frame_path, "")
            if ocr_text:
                safe_text = ocr_text.replace("```", "'''")
                lines.extend(["**OCR**", "", "```text", safe_text, "```", ""])
        else:
            start = format_timestamp(float(payload["start_seconds"]))
            end = format_timestamp(float(payload["end_seconds"]))
            lines.extend([f"## {start} - {end} - Speech", "", str(payload["text"]), ""])

    timeline_path = out_dir / "timeline.md"
    timeline_path.write_text("\n".join(lines), encoding="utf-8")
    return "timeline.md"


def build_manifest(
    source: Path,
    source_id: str | None,
    media: dict[str, Any],
    keyframes: list[dict[str, Any]],
    keyframe_stats: dict[str, Any],
    timeline: str,
    contact_sheet: str | None,
    ocr_artifact: str | None,
    ocr_stats: dict[str, Any],
    transcript_artifact: str | None,
    transcript_json: str | None,
    transcript_stats: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"

    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "provider": "local_test" if source_id is None else "external",
            "file_id": source_id,
            "sha256": sha256_file(source),
            "name": source.name,
            "media_type": mime_type,
        },
        "media": media,
        "processing": {
            "status": "processed",
            "pipeline_version": PIPELINE_VERSION,
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "warnings": sorted(set(warnings)),
            "keyframe_stats": keyframe_stats,
            "ocr_stats": ocr_stats,
            "transcript_stats": transcript_stats,
        },
        "artifacts": {
            "timeline": timeline,
            "transcript": transcript_artifact,
            "transcript_json": transcript_json,
            "ocr": ocr_artifact,
            "contact_sheet": contact_sheet,
            "keyframes": keyframes,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate compact preprocessing artifacts for a video source.")
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--max-keyframes", type=int, default=DEFAULT_MAX_KEYFRAMES)
    parser.add_argument("--scene-threshold", type=float, default=DEFAULT_SCENE_THRESHOLD)
    parser.add_argument("--dedupe-distance", type=int, default=DEFAULT_DEDUPE_DISTANCE)
    parser.add_argument("--ocr", action="store_true", help="Run Tesseract on selected keyframes.")
    parser.add_argument("--ocr-languages", default=DEFAULT_LANGUAGES)
    parser.add_argument("--transcribe", action="store_true", help="Run faster-whisper when audio exists.")
    parser.add_argument("--whisper-model", default=DEFAULT_MODEL)
    parser.add_argument("--language", default=None, help="Optional ISO language hint for Whisper.")
    args = parser.parse_args()

    source = args.source.resolve()
    if not source.is_file():
        parser.error("source must be an existing file")
    if not 1 <= args.max_keyframes <= 50:
        parser.error("--max-keyframes must be between 1 and 50")
    if not 0.01 <= args.scene_threshold <= 1.0:
        parser.error("--scene-threshold must be between 0.01 and 1.0")
    if not 0 <= args.dedupe_distance <= 64:
        parser.error("--dedupe-distance must be between 0 and 64")

    args.out.mkdir(parents=True, exist_ok=True)
    probe = ffprobe(source)
    media = video_metadata(probe)
    duration = media.get("duration_seconds") or 0.0

    keyframes, keyframe_stats = build_keyframes(
        source,
        args.out,
        duration,
        args.max_keyframes,
        args.scene_threshold,
        args.dedupe_distance,
    )

    warnings: list[str] = []
    contact_sheet = build_contact_sheet(args.out, keyframes)
    if keyframes and contact_sheet is None:
        warnings.append("contact_sheet_generation_failed")

    ocr_artifact: str | None = None
    ocr_by_frame: dict[str, str] = {}
    ocr_stats: dict[str, Any] = {
        "enabled": False,
        "languages": None,
        "frames_attempted": 0,
        "frames_with_text": 0,
    }
    if args.ocr:
        ocr_artifact, ocr_by_frame, ocr_stats, ocr_warnings = build_ocr_artifact(
            args.out,
            keyframes,
            requested_languages=args.ocr_languages,
        )
        warnings.extend(ocr_warnings)

    transcript_artifact: str | None = None
    transcript_json: str | None = None
    transcript_segments: list[dict[str, Any]] = []
    transcript_stats: dict[str, Any] = {
        "enabled": False,
        "model": None,
        "detected_language": None,
        "language_probability": None,
        "segments": 0,
    }
    if args.transcribe:
        if not media.get("has_audio"):
            transcript_stats = {
                "enabled": True,
                "model": args.whisper_model,
                "detected_language": None,
                "language_probability": None,
                "segments": 0,
                "skipped_reason": "no_audio",
            }
        else:
            (
                transcript_artifact,
                transcript_json,
                transcript_segments,
                transcript_stats,
                transcript_warnings,
            ) = build_transcript_artifact(
                source,
                args.out,
                model_name=args.whisper_model,
                language=args.language,
            )
            warnings.extend(transcript_warnings)

    timeline = write_timeline(
        args.out,
        media,
        keyframes,
        contact_sheet,
        ocr_by_frame,
        transcript_segments,
    )

    manifest = build_manifest(
        source,
        args.source_id,
        media,
        keyframes,
        keyframe_stats,
        timeline,
        contact_sheet,
        ocr_artifact,
        ocr_stats,
        transcript_artifact,
        transcript_json,
        transcript_stats,
        warnings,
    )
    output = args.out / "ingest.json"
    output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        "ingest_ok "
        f"duration={media['duration_seconds']} "
        f"resolution={media['width']}x{media['height']} "
        f"has_audio={str(media['has_audio']).lower()} "
        f"keyframes={len(keyframes)} "
        f"timeline={str(bool(timeline)).lower()} "
        f"contact_sheet={str(bool(contact_sheet)).lower()} "
        f"ocr={str(bool(ocr_artifact)).lower()} "
        f"transcript={str(bool(transcript_artifact)).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
