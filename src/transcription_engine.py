#!/usr/bin/env python3
"""Optional CPU transcription for Technology Library media inputs.

The dependency is imported lazily so non-transcription pipelines remain usable
without faster-whisper installed. The model can be a Hugging Face model name or
a local model directory; production can therefore move to pre-fetched local
models.

Decode-quality metrics produced by faster-whisper are preserved as mechanical
signals only. They help the curator decide when speech evidence deserves extra
scrutiny but are not a semantic accuracy score.

Whisper models are cached in memory per Python process. This allows a media
bridge to process several files without reloading the same CPU/int8 model for
each item.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "base"
MAX_SEGMENTS = 2000
MAX_SEGMENT_CHARS = 4000
WHITESPACE_RE = re.compile(r"[ \t]+")

LOGPROB_SUSPECT_THRESHOLD = -1.0
NO_SPEECH_SUSPECT_THRESHOLD = 0.60
COMPRESSION_SUSPECT_THRESHOLD = 2.40

_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}


def normalize_text(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text.replace("\r", " ").replace("\n", " ")).strip()[:MAX_SEGMENT_CHARS]


def format_timestamp(seconds: float) -> str:
    milliseconds = int(round(max(0.0, seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def safe_metric(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return round(number, 4)


def segment_quality(segment: Any) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for output_key, attribute in (
        ("avg_logprob", "avg_logprob"),
        ("no_speech_prob", "no_speech_prob"),
        ("compression_ratio", "compression_ratio"),
    ):
        value = safe_metric(getattr(segment, attribute, None))
        if value is not None:
            metrics[output_key] = value
    return metrics


def summarize_quality(records: list[dict[str, Any]]) -> dict[str, Any]:
    metric_records = [record.get("quality") for record in records if isinstance(record.get("quality"), dict)]
    metric_records = [record for record in metric_records if record]

    logprobs = [float(record["avg_logprob"]) for record in metric_records if record.get("avg_logprob") is not None]
    no_speech = [float(record["no_speech_prob"]) for record in metric_records if record.get("no_speech_prob") is not None]
    compression = [float(record["compression_ratio"]) for record in metric_records if record.get("compression_ratio") is not None]

    suspect_segments = 0
    for record in metric_records:
        logprob = record.get("avg_logprob")
        no_speech_prob = record.get("no_speech_prob")
        compression_ratio = record.get("compression_ratio")
        if (
            (logprob is not None and float(logprob) < LOGPROB_SUSPECT_THRESHOLD)
            or (no_speech_prob is not None and float(no_speech_prob) > NO_SPEECH_SUSPECT_THRESHOLD)
            or (compression_ratio is not None and float(compression_ratio) > COMPRESSION_SUSPECT_THRESHOLD)
        ):
            suspect_segments += 1

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    measured = len(metric_records)
    return {
        "segments_with_metrics": measured,
        "suspect_segments": suspect_segments,
        "suspect_ratio": round(suspect_segments / measured, 4) if measured else None,
        "mean_avg_logprob": mean(logprobs),
        "mean_no_speech_prob": mean(no_speech),
        "mean_compression_ratio": mean(compression),
    }


def empty_stats(model_name: str) -> dict[str, Any]:
    return {
        "enabled": True,
        "model": model_name,
        "detected_language": None,
        "language_probability": None,
        "segments": 0,
        "segments_with_metrics": 0,
        "suspect_segments": 0,
        "suspect_ratio": None,
        "mean_avg_logprob": None,
        "mean_no_speech_prob": None,
        "mean_compression_ratio": None,
    }


def get_whisper_model(model_name: str) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("transcription_unavailable") from exc

    key = (model_name, "cpu", "int8")
    model = _MODEL_CACHE.get(key)
    if model is None:
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        _MODEL_CACHE[key] = model
    return model


def build_transcript_artifact(
    source: Path,
    out_dir: Path,
    model_name: str = DEFAULT_MODEL,
    language: str | None = None,
) -> tuple[str | None, str | None, list[dict[str, Any]], dict[str, Any], list[str]]:
    warnings: list[str] = []
    try:
        model = get_whisper_model(model_name)
    except RuntimeError as exc:
        if str(exc) == "transcription_unavailable":
            warnings.append("transcription_unavailable")
            return None, None, [], empty_stats(model_name), warnings
        warnings.append("transcription_failed")
        return None, None, [], empty_stats(model_name), warnings
    except Exception:
        warnings.append("transcription_failed")
        return None, None, [], empty_stats(model_name), warnings

    try:
        segments_iter, info = model.transcribe(
            str(source),
            beam_size=1,
            language=language,
            vad_filter=True,
            condition_on_previous_text=False,
        )

        records: list[dict[str, Any]] = []
        for segment in segments_iter:
            text = normalize_text(segment.text)
            if not text:
                continue
            record: dict[str, Any] = {
                "start_seconds": round(float(segment.start), 3),
                "end_seconds": round(float(segment.end), 3),
                "text": text,
            }
            quality = segment_quality(segment)
            if quality:
                record["quality"] = quality
            records.append(record)
            if len(records) >= MAX_SEGMENTS:
                warnings.append("transcript_segment_limit_reached")
                break

        detected_language = getattr(info, "language", None)
        probability = safe_metric(getattr(info, "language_probability", None))
        quality_summary = summarize_quality(records)

        structured = {
            "schema_version": 2,
            "engine": "faster-whisper",
            "model": model_name,
            "detected_language": detected_language,
            "language_probability": probability,
            "quality_summary": quality_summary,
            "segments": records,
        }
        json_path = out_dir / "transcript.json"
        json_path.write_text(
            json.dumps(structured, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        lines = ["# Transcript", ""]
        for record in records:
            start = format_timestamp(float(record["start_seconds"]))
            end = format_timestamp(float(record["end_seconds"]))
            lines.extend([f"## {start} - {end}", "", record["text"], ""])
        markdown_path = out_dir / "transcript.md"
        markdown_path.write_text("\n".join(lines), encoding="utf-8")

        stats = {
            "enabled": True,
            "model": model_name,
            "detected_language": detected_language,
            "language_probability": probability,
            "segments": len(records),
            **quality_summary,
        }
        return "transcript.md", "transcript.json", records, stats, sorted(set(warnings))
    except Exception:
        # Do not leak third-party exception text into normal CI logs because it
        # can include local paths or model/cache details. Debugging can inspect
        # an isolated synthetic run when needed.
        warnings.append("transcription_failed")
        return None, None, [], empty_stats(model_name), warnings
