#!/usr/bin/env python3
"""Mechanical local audio ingestion for Technology Library.

Audio V1 creates technical metadata, a compact time-spread Opus preview and,
optionally, a faster-whisper transcript. The preview is always required because
speech transcription cannot represent music, sound effects or ambient audio.

No paid API or external LLM is used. Normal stdout contains only bounded
technical status and never private filenames or transcript text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evidence_compactor import bounded_speech, transcript_quality
from transcription_engine import DEFAULT_MODEL, build_transcript_artifact, empty_stats

AUDIO_PIPELINE_VERSION = "0.1.0"
AUDIO_SUMMARY_VERSION = "0.1.0"
SCHEMA_VERSION = 1
MAX_SUMMARY_BYTES = 3072
MAX_INPUT_BYTES = 500 * 1024 * 1024
MAX_DURATION_SECONDS = 8 * 60 * 60
MAX_TRANSCRIBE_SECONDS = 30 * 60
PREVIEW_SEGMENT_SECONDS = 20.0
PREVIEW_SHORT_FULL_SECONDS = 60.0
PREVIEW_SAMPLE_RATE = 16_000
PREVIEW_BITRATE = "24k"
SUPPORTED_SUFFIXES = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}


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
        speech = evidence.get("speech")
        if isinstance(speech, list):
            for limit in (4, 2, 1, 0):
                evidence["speech"] = speech[:limit]
                if encoded_size(payload) <= MAX_SUMMARY_BYTES:
                    return payload
        payload.pop("evidence", None)
    if encoded_size(payload) > MAX_SUMMARY_BYTES:
        payload = {
            "schema_version": payload.get("schema_version"),
            "summary_version": payload.get("summary_version"),
            "source": payload.get("source"),
            "audio": payload.get("audio"),
            "quality": payload.get("quality"),
            "audit": payload.get("audit"),
        }
    return payload


def run_text(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def probe_audio(path: Path) -> dict[str, Any]:
    result = run_text([
        "ffprobe",
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries",
        "stream=codec_name,sample_rate,channels,channel_layout,sample_fmt,bit_rate:format=format_name,duration,size,bit_rate",
        "-of", "json",
        str(path),
    ])
    if result.returncode != 0:
        raise RuntimeError("audio_probe_failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("audio_probe_invalid_json") from exc

    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list) or not streams or not isinstance(streams[0], dict):
        raise RuntimeError("audio_stream_missing")
    stream = streams[0]
    format_payload = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration = safe_float(format_payload.get("duration"))
    if duration is None or duration <= 0:
        raise RuntimeError("audio_duration_missing")
    if duration > MAX_DURATION_SECONDS:
        raise RuntimeError("audio_duration_exceeded")

    def safe_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return {
        "duration_seconds": round(duration, 3),
        "codec": stream.get("codec_name"),
        "sample_rate": safe_int(stream.get("sample_rate")),
        "channels": safe_int(stream.get("channels")),
        "channel_layout": stream.get("channel_layout"),
        "sample_format": stream.get("sample_fmt"),
        "stream_bit_rate": safe_int(stream.get("bit_rate")),
        "container_bit_rate": safe_int(format_payload.get("bit_rate")),
        "container_format": format_payload.get("format_name"),
    }


def preview_intervals(duration: float) -> list[dict[str, float]]:
    if duration <= PREVIEW_SHORT_FULL_SECONDS:
        return [{"start": 0.0, "duration": round(duration, 3)}]

    segment = min(PREVIEW_SEGMENT_SECONDS, duration / 3.0)
    middle = max(segment, (duration - segment) / 2.0)
    end = max(0.0, duration - segment)
    starts = [0.0, middle, end]
    output: list[dict[str, float]] = []
    last_end = -1.0
    for start in starts:
        start = max(0.0, min(float(start), duration - segment))
        if start < last_end:
            start = last_end
        if start >= duration:
            break
        current_duration = min(segment, duration - start)
        output.append({"start": round(start, 3), "duration": round(current_duration, 3)})
        last_end = start + current_duration
    return output


def make_preview(source: Path, destination: Path, intervals: list[dict[str, float]]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tl-audio-preview-") as temp_name:
        temp = Path(temp_name)
        parts: list[Path] = []
        for index, interval in enumerate(intervals):
            part = temp / f"part-{index:02d}.opus"
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-ss", str(interval["start"]),
                "-t", str(interval["duration"]),
                "-i", str(source),
                "-map_metadata", "-1",
                "-vn",
                "-ac", "1",
                "-ar", str(PREVIEW_SAMPLE_RATE),
                "-c:a", "libopus",
                "-b:a", PREVIEW_BITRATE,
                "-vbr", "on",
                "-application", "audio",
                "-y", str(part),
            ]
            result = run_text(command)
            if result.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
                raise RuntimeError("audio_preview_segment_failed")
            parts.append(part)

        if len(parts) == 1:
            shutil.copyfile(parts[0], destination)
        else:
            concat = temp / "concat.txt"
            concat.write_text(
                "".join(f"file '{part.as_posix()}'\n" for part in parts),
                encoding="utf-8",
            )
            result = run_text([
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0",
                "-i", str(concat),
                "-c", "copy",
                "-map_metadata", "-1",
                "-y", str(destination),
            ])
            if result.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
                raise RuntimeError("audio_preview_concat_failed")


def transcription_state(
    *,
    enabled: bool,
    duration: float,
    source: Path,
    out: Path,
    model_name: str,
    language: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str], dict[str, str | None]]:
    if not enabled:
        return [], {**empty_stats(model_name), "enabled": False}, [], {"markdown": None, "json": None}
    if duration > MAX_TRANSCRIBE_SECONDS:
        stats = empty_stats(model_name)
        stats["enabled"] = True
        stats["skipped"] = True
        stats["skip_reason"] = "duration_limit"
        return [], stats, ["transcription_skipped_duration_limit"], {"markdown": None, "json": None}

    markdown, structured, records, stats, warnings = build_transcript_artifact(
        source,
        out,
        model_name=model_name,
        language=language,
    )
    return records, stats, warnings, {"markdown": markdown, "json": structured}


def unknown_transcript_quality(basis: str) -> dict[str, Any]:
    return {
        "signal": "unknown",
        "basis": basis,
        "detected_language": None,
        "language_probability": None,
        "segments": 0,
        "segments_with_metrics": 0,
        "suspect_segments": 0,
        "suspect_ratio": None,
        "mean_avg_logprob": None,
        "mean_no_speech_prob": None,
    }


def build_quality(*, requested: bool, stats: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    if not requested:
        return unknown_transcript_quality("not_requested")
    if "transcription_skipped_duration_limit" in warnings:
        return unknown_transcript_quality("duration_limit")
    if "transcription_unavailable" in warnings or "transcription_failed" in warnings:
        return unknown_transcript_quality("transcription_unavailable_or_failed")
    media = {"has_audio": True}
    return transcript_quality(media, stats)


def ingest_audio(
    source: Path,
    out: Path,
    *,
    source_id: str | None,
    transcribe: bool,
    model_name: str,
    language: str | None,
) -> dict[str, Any]:
    if not source.is_file():
        raise RuntimeError("source_missing")
    if source.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise RuntimeError("unsupported_audio_format")
    size = source.stat().st_size
    if size < 1:
        raise RuntimeError("source_empty")
    if size > MAX_INPUT_BYTES:
        raise RuntimeError("source_too_large")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("ffmpeg_missing")

    out.mkdir(parents=True, exist_ok=True)
    source_sha = sha256_file(source)
    metadata = probe_audio(source)
    duration = float(metadata["duration_seconds"])
    intervals = preview_intervals(duration)
    preview = out / "preview.opus"
    make_preview(source, preview, intervals)

    records, transcript_stats, warnings, transcript_artifacts = transcription_state(
        enabled=transcribe,
        duration=duration,
        source=source,
        out=out,
        model_name=model_name,
        language=language,
    )
    speech, speech_truncated = bounded_speech(records)
    quality = build_quality(requested=transcribe, stats=transcript_stats, warnings=warnings)

    index = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": AUDIO_PIPELINE_VERSION,
        "source": {"sha256": source_sha, "bytes": size},
        "audio": metadata,
        "preview": {
            "artifact": "preview.opus",
            "sha256": sha256_file(preview),
            "sample_rate": PREVIEW_SAMPLE_RATE,
            "channels": 1,
            "codec": "opus",
            "bitrate": PREVIEW_BITRATE,
            "intervals": intervals,
        },
        "transcription": {
            "requested": transcribe,
            "artifacts": transcript_artifacts,
            "stats": transcript_stats,
            "warnings": warnings,
            "automatic_duration_limit_seconds": MAX_TRANSCRIBE_SECONDS,
        },
        "privacy": {
            "embedded_metadata_exported": False,
            "preview_metadata_stripped": True,
        },
    }
    write_json(out / "audio-index.json", index)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": AUDIO_PIPELINE_VERSION,
        "created_at": utc_now(),
        "source": {
            "provider": "external",
            "file_id": source_id,
            "sha256": source_sha,
            "bytes": size,
        },
        "audio": metadata,
        "artifacts": {
            "index": "audio-index.json",
            "preview": "preview.opus",
            "transcript": transcript_artifacts["markdown"],
            "transcript_json": transcript_artifacts["json"],
        },
        "processing": {
            "transcript_stats": transcript_stats,
            "warnings": warnings,
        },
    }
    write_json(out / "ingest.json", manifest)

    summary = compact_summary({
        "schema_version": SCHEMA_VERSION,
        "summary_version": AUDIO_SUMMARY_VERSION,
        "source": {
            "provider": "external",
            "file_id": source_id,
            "sha256": source_sha,
        },
        "audio": {
            "duration_seconds": metadata["duration_seconds"],
            "codec": metadata.get("codec"),
            "sample_rate": metadata.get("sample_rate"),
            "channels": metadata.get("channels"),
            "preview_intervals": intervals,
            "transcription_requested": transcribe,
            "transcription_skipped": "transcription_skipped_duration_limit" in warnings,
        },
        "quality": {"transcript": quality},
        "evidence": {
            "speech": speech,
            "speech_truncated": speech_truncated,
        },
        "audit": {
            "manifest": "ingest.json",
            "index": "audio-index.json",
            "preview": "preview.opus",
            "transcript": transcript_artifacts["markdown"],
            "transcript_json": transcript_artifacts["json"],
        },
    })
    write_json(out / "evidence-summary.json", summary, compact=True)
    if (out / "evidence-summary.json").stat().st_size > MAX_SUMMARY_BYTES:
        raise RuntimeError("summary_budget_exceeded")

    checkpoint = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": AUDIO_PIPELINE_VERSION,
        "summary_version": AUDIO_SUMMARY_VERSION,
        "source_sha256": source_sha,
        "status": "complete",
        "required_artifacts": [
            "ingest.json",
            "checkpoint.json",
            "audio-index.json",
            "evidence-summary.json",
            "preview.opus",
        ],
    }
    write_json(out / "checkpoint.json", checkpoint)

    return {
        "duration_seconds": metadata["duration_seconds"],
        "preview_intervals": len(intervals),
        "transcript_segments": len(records),
        "transcription_skipped": "transcription_skipped_duration_limit" in warnings,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create compact mechanical evidence from a supported audio file.")
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-id")
    parser.add_argument("--transcribe", action="store_true")
    parser.add_argument("--whisper-model", default=DEFAULT_MODEL)
    parser.add_argument("--language")
    args = parser.parse_args()

    try:
        result = ingest_audio(
            args.source.resolve(),
            args.out.resolve(),
            source_id=args.source_id,
            transcribe=args.transcribe,
            model_name=args.whisper_model,
            language=args.language,
        )
    except RuntimeError as exc:
        print(f"audio_ingest_error code={str(exc) or 'audio_processing_failed'}")
        return 2
    except (OSError, ValueError, OverflowError):
        print("audio_ingest_error code=unexpected_local_error")
        return 2

    print(
        "audio_ingest_ok "
        f"duration={result['duration_seconds']} preview_intervals={result['preview_intervals']} "
        f"segments={result['transcript_segments']} skipped={1 if result['transcription_skipped'] else 0}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())