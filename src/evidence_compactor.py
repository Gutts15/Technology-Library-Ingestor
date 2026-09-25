#!/usr/bin/env python3
"""Mechanical evidence compaction for Technology Library ingestion packages.

This stage intentionally does not interpret or classify technologies. It only:
- marks transcript quality with a conservative heuristic signal
- suppresses near-duplicate OCR records
- keeps bounded, time-spread speech evidence
- enforces a hard byte budget for evidence-summary.json
- points the curator to the smaller normal-read artifact

Only Python's standard library is used.
"""

from __future__ import annotations

import argparse
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

COMPACTOR_VERSION = "0.6.2"
SCHEMA_VERSION = 1
MAX_OCR_ITEMS = 5
MAX_SPEECH_ITEMS = 6
MAX_OCR_TOTAL_CHARS = 1200
MAX_SPEECH_TOTAL_CHARS = 1400
MAX_ITEM_CHARS = 360
MAX_SUMMARY_BYTES = 3000
WORD_RE = re.compile(r"[\w#+.:-]+", re.UNICODE)
SPACE_RE = re.compile(r"\s+")


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def compact_json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def write_compact_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_bytes(compact_json_bytes(payload))


def comparison_text(text: str) -> str:
    return " ".join(WORD_RE.findall(SPACE_RE.sub(" ", text.lower())))[:3000]


def compact_text(text: str, max_chars: int) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for raw in text.replace("\r", "\n").split("\n"):
        line = SPACE_RE.sub(" ", raw).strip()
        if not line:
            continue
        signature = comparison_text(line)
        if not signature or signature in seen:
            continue
        seen.add(signature)
        lines.append(line)
    return "\n".join(lines)[:max_chars]


def text_similarity(left: str, right: str) -> float:
    left_norm = comparison_text(left)
    right_norm = comparison_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    sequence = SequenceMatcher(None, left_norm, right_norm, autojunk=False).ratio()
    return max(jaccard, sequence)


def dedupe_ocr_records(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    kept: list[dict[str, Any]] = []
    suppressed = 0
    for record in records:
        text = compact_text(str(record.get("text") or ""), 3000)
        if not text:
            continue
        if any(text_similarity(text, str(existing.get("text") or "")) >= 0.86 for existing in kept):
            suppressed += 1
            continue
        kept.append({**record, "text": text})
    return kept, suppressed


def spread_indexes(length: int, limit: int) -> list[int]:
    if length <= 0 or limit <= 0:
        return []
    if length <= limit:
        return list(range(length))
    if limit == 1:
        return [0]
    last = length - 1
    return sorted({round(i * last / (limit - 1)) for i in range(limit)})


def bounded_ocr(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    selected = [records[i] for i in spread_indexes(len(records), MAX_OCR_ITEMS)]
    remaining = MAX_OCR_TOTAL_CHARS
    output: list[dict[str, Any]] = []
    truncated = len(selected) < len(records)
    for record in selected:
        if remaining <= 0:
            truncated = True
            break
        text = compact_text(str(record.get("text") or ""), min(MAX_ITEM_CHARS, remaining))
        if not text:
            continue
        output.append({"t": record.get("timestamp_seconds"), "text": text})
        remaining -= len(text)
    return output, truncated


def bounded_speech(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    selected = [records[i] for i in spread_indexes(len(records), MAX_SPEECH_ITEMS)]
    remaining = MAX_SPEECH_TOTAL_CHARS
    output: list[dict[str, Any]] = []
    truncated = len(selected) < len(records)
    for record in selected:
        if remaining <= 0:
            truncated = True
            break
        text = compact_text(str(record.get("text") or ""), min(MAX_ITEM_CHARS, remaining))
        if not text:
            continue
        output.append({
            "start": record.get("start_seconds"),
            "end": record.get("end_seconds"),
            "text": text,
        })
        remaining -= len(text)
    return output, truncated


def safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def transcript_quality(media: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    """Return a cautious routing signal, not a semantic accuracy claim.

    New packages use faster-whisper decode metrics. Old packages that do not
    contain those metrics keep the previous language-probability fallback so
    the compactor remains backward-compatible.
    """
    has_audio = bool(media.get("has_audio"))
    segments = int(stats.get("segments") or 0)
    language = stats.get("detected_language")
    language_probability = safe_float(stats.get("language_probability"))
    measured = int(stats.get("segments_with_metrics") or 0)
    suspect_segments = int(stats.get("suspect_segments") or 0)
    suspect_ratio = safe_float(stats.get("suspect_ratio"))
    mean_logprob = safe_float(stats.get("mean_avg_logprob"))
    mean_no_speech = safe_float(stats.get("mean_no_speech_prob"))
    mean_compression = safe_float(stats.get("mean_compression_ratio"))

    if not has_audio:
        signal = "not_applicable"
        basis = "no_audio"
    elif segments == 0:
        signal = "low"
        basis = "no_speech_segments"
    elif measured > 0:
        basis = "whisper_decode_metrics"
        low_reasons = [
            suspect_ratio is not None and suspect_ratio >= 0.50,
            mean_logprob is not None and mean_logprob < -1.0,
            mean_no_speech is not None and mean_no_speech > 0.60,
            mean_compression is not None and mean_compression > 2.40,
        ]
        high_reasons = [
            suspect_ratio is not None and suspect_ratio <= 0.10,
            mean_logprob is not None and mean_logprob >= -0.60,
            mean_no_speech is not None and mean_no_speech <= 0.25,
            language_probability is None or language_probability >= 0.75,
        ]
        if any(low_reasons):
            signal = "low"
        elif all(high_reasons):
            signal = "high"
        else:
            signal = "medium"
    elif language_probability is None:
        signal = "unknown"
        basis = "legacy_missing_metrics"
    else:
        basis = "language_detection_probability_proxy"
        signal = "low" if language_probability < 0.60 else "medium" if language_probability < 0.80 else "high"

    return {
        "signal": signal,
        "basis": basis,
        "detected_language": language,
        "language_probability": round(language_probability, 4) if language_probability is not None else None,
        "segments": segments,
        "segments_with_metrics": measured,
        "suspect_segments": suspect_segments,
        "suspect_ratio": round(suspect_ratio, 4) if suspect_ratio is not None else None,
        "mean_avg_logprob": round(mean_logprob, 4) if mean_logprob is not None else None,
        "mean_no_speech_prob": round(mean_no_speech, 4) if mean_no_speech is not None else None,
    }


def trim_to_budget(summary: dict[str, Any]) -> bool:
    trimmed = False
    evidence = summary["evidence"]
    while len(compact_json_bytes(summary)) > MAX_SUMMARY_BYTES:
        ocr_items = evidence["ocr"]
        speech_items = evidence["speech"]
        candidates: list[tuple[int, str, int]] = []
        for index, item in enumerate(ocr_items):
            candidates.append((len(str(item.get("text") or "")), "ocr", index))
        for index, item in enumerate(speech_items):
            candidates.append((len(str(item.get("text") or "")), "speech", index))
        if not candidates:
            break
        _, kind, index = max(candidates)
        evidence[kind].pop(index)
        trimmed = True
    return trimmed


def finalize_routing(summary: dict[str, Any], package_dir: Path) -> bytes:
    timeline_path = package_dir / "timeline.md"
    timeline_bytes = timeline_path.stat().st_size if timeline_path.is_file() else None
    summary["routing"] = {
        "preferred_artifact": "evidence-summary.json",
        "summary_bytes": 0,
        "timeline_bytes": timeline_bytes,
    }
    for _ in range(4):
        data = compact_json_bytes(summary)
        summary["routing"]["summary_bytes"] = len(data)
        summary["routing"]["preferred_artifact"] = (
            "evidence-summary.json"
            if timeline_bytes is None or len(data) <= timeline_bytes
            else "timeline.md"
        )
    return compact_json_bytes(summary)


def build_evidence_summary(package_dir: Path) -> dict[str, Any]:
    ingest = read_json(package_dir / "ingest.json")
    if not ingest:
        raise ValueError("missing_ingest_manifest")

    media = ingest.get("media") if isinstance(ingest.get("media"), dict) else {}
    source = ingest.get("source") if isinstance(ingest.get("source"), dict) else {}
    processing = ingest.get("processing") if isinstance(ingest.get("processing"), dict) else {}
    artifacts = ingest.get("artifacts") if isinstance(ingest.get("artifacts"), dict) else {}
    transcript_stats = processing.get("transcript_stats") if isinstance(processing.get("transcript_stats"), dict) else {}

    ocr = read_json(package_dir / "ocr.json") or {}
    raw_ocr = ocr.get("records") if isinstance(ocr.get("records"), list) else []
    ocr_records = [record for record in raw_ocr if isinstance(record, dict)]
    unique_ocr, duplicate_count = dedupe_ocr_records(ocr_records)
    compact_ocr, ocr_truncated = bounded_ocr(unique_ocr)

    transcript = read_json(package_dir / "transcript.json") or {}
    raw_speech = transcript.get("segments") if isinstance(transcript.get("segments"), list) else []
    speech_records = [record for record in raw_speech if isinstance(record, dict)]
    compact_speech, speech_truncated = bounded_speech(speech_records)

    quality = transcript_quality(media, transcript_stats)
    warnings: list[str] = []
    if quality["signal"] == "low":
        warnings.append("transcript_low_quality_signal")
    if duplicate_count:
        warnings.append("ocr_near_duplicates_suppressed")
    if ocr_truncated:
        warnings.append("ocr_summary_bounded")
    if speech_truncated:
        warnings.append("speech_summary_bounded")

    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "compactor_version": COMPACTOR_VERSION,
        "source": {
            "provider": source.get("provider"),
            "file_id": source.get("file_id"),
            "media_type": source.get("media_type"),
        },
        "media": {
            "duration_seconds": media.get("duration_seconds"),
            "has_audio": media.get("has_audio"),
        },
        "quality": {
            "transcript": quality,
            "ocr": {
                "engine": ocr.get("engine"),
                "languages": ocr.get("languages"),
                "records_total": len(ocr_records),
                "records_unique": len(unique_ocr),
                "near_duplicates_suppressed": duplicate_count,
            },
        },
        "evidence": {"ocr": compact_ocr, "speech": compact_speech},
        "full": {
            "timeline": artifacts.get("timeline") or "timeline.md",
            "ocr": artifacts.get("ocr") or ("ocr.json" if (package_dir / "ocr.json").is_file() else None),
            "transcript": artifacts.get("transcript") or ("transcript.md" if (package_dir / "transcript.md").is_file() else None),
            "contact_sheet": artifacts.get("contact_sheet") or ("contact-sheet.jpg" if (package_dir / "contact-sheet.jpg").is_file() else None),
        },
        "warnings": warnings,
    }

    if trim_to_budget(summary):
        warnings.append("summary_byte_budget_trimmed")
    summary["warnings"] = sorted(set(warnings))
    if len(compact_json_bytes(summary)) > MAX_SUMMARY_BYTES:
        raise ValueError("summary_byte_budget_unreachable")

    data = finalize_routing(summary, package_dir)
    if len(data) > MAX_SUMMARY_BYTES:
        summary["routing"] = {"preferred_artifact": "timeline.md", "summary_bytes": 0, "timeline_bytes": summary["routing"].get("timeline_bytes")}
        if trim_to_budget(summary):
            summary["warnings"] = sorted(set([*summary["warnings"], "summary_byte_budget_trimmed"]))
        data = finalize_routing(summary, package_dir)
    if len(data) > MAX_SUMMARY_BYTES:
        raise ValueError("summary_byte_budget_unreachable")

    (package_dir / "evidence-summary.json").write_bytes(data)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a compact evidence summary for one ingestion package.")
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    build_evidence_summary(args.package.resolve())
    print("evidence_compaction_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
