#!/usr/bin/env python3
"""Bounded local E1 extraction using the existing format processors.

This adapter does not read Drive, call a model, publish or mutate an index. It
returns the existing FILE_EVIDENCE envelope so a coordinated caller can persist
it. All decoding runs in a killable process group, with a shared wall deadline,
disk/output bounds and no inherited credentials. Links without fetched content
are HELD; a URL alone is never represented as extracted evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evidence_quality import assess_evidence
from ready_evidence_bridge import build_envelope, semantic_summary
from text_document_ingest import DOCUMENT_SUFFIXES, TEXT_SUFFIXES

MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_FILES = 4096
MAX_SUMMARY_BYTES = 3072
MAX_ARCHIVE_ENTRIES = 10000
SRC = Path(__file__).resolve().parent
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"}


class ExtractionError(Exception):
    """A fixed, privacy-safe error code; never a raw decoder exception."""


@dataclass(frozen=True)
class ExtractedSource:
    package_id: str
    revision_key: str
    kind: str
    source_sha256: str
    evidence_raw: bytes
    envelope: dict[str, Any]
    artifact_directory: Path

    @property
    def assessment(self) -> dict[str, Any]:
        return self.envelope["evidence_assessment"]


def encode(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _kind(suffix: str) -> str:
    if suffix in TEXT_SUFFIXES:
        return "text"
    if suffix in DOCUMENT_SUFFIXES:
        return "document"
    if suffix in {".csv", ".tsv", ".xlsx", ".xlsm"}:
        return "spreadsheet"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in {".url", ".webloc"}:
        return "link"
    return "unknown"


def _safe_absolute(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    if absolute.resolve() != absolute:
        raise ExtractionError("symlink_path_forbidden")
    return absolute


def _stop_group(proc: subprocess.Popen) -> None:
    # Kill descendants as well as the decoder, including a blocked ffmpeg or
    # tesseract. Reap before permitting any subsequent artifact read.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=5)


def _artifact_bytes(root: Path) -> int:
    total = 0
    for count, path in enumerate(root.rglob("*"), start=1):
        if count > MAX_ARTIFACT_FILES:
            raise ExtractionError("artifact_count_exceeded")
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ExtractionError("artifact_symlink_forbidden")
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
        elif not stat.S_ISDIR(info.st_mode):
            raise ExtractionError("artifact_type_forbidden")
        if total > MAX_ARTIFACT_BYTES:
            raise ExtractionError("artifact_budget_exceeded")
    return total


def _run(script: str, arguments: list[str], output: Path, deadline: float) -> bool:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ExtractionError("extraction_deadline")
    # Resource limits are inherited by nested decoders. The default production
    # executor is Linux Actions; fail closed on platforms lacking this boundary.
    if os.name != "posix":
        raise ExtractionError("bounded_decoder_platform_unavailable")
    bootstrap = (
        "import resource,runpy,sys;"
        f"resource.setrlimit(resource.RLIMIT_FSIZE,({MAX_INPUT_BYTES},{MAX_INPUT_BYTES}));"
        "resource.setrlimit(resource.RLIMIT_AS,(3221225472,3221225472));"
        f"resource.setrlimit(resource.RLIMIT_CPU,({max(1, math.ceil(remaining))},{max(1, math.ceil(remaining))}));"
        "resource.setrlimit(resource.RLIMIT_NOFILE,(256,256));"
        "p=sys.argv[1];sys.argv=sys.argv[1:];"
        "sys.path.insert(0,__import__('os').path.dirname(p));"
        "runpy.run_path(p,run_name='__main__')"
    )
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT")
           if key in os.environ}
    env.update({"HF_HOME": str(output / "model-cache"),
                "XDG_CACHE_HOME": str(output / "cache"), "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1"})
    try:
        proc = subprocess.Popen([sys.executable, "-c", bootstrap, str(SRC / script),
                                 *arguments], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=env, start_new_session=True)
    except OSError:
        raise ExtractionError("decoder_start_failed") from None
    try:
        while proc.poll() is None:
            _artifact_bytes(output)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ExtractionError("extraction_deadline")
            time.sleep(min(0.05, remaining))
        succeeded = proc.returncode == 0
    finally:
        _stop_group(proc)
    _artifact_bytes(output)
    return succeeded


def _archive_safe(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            return (len(members) <= MAX_ARCHIVE_ENTRIES and
                    sum(member.file_size for member in members) <= MAX_ARTIFACT_BYTES and
                    not any(member.flag_bits & 1 for member in members))
    except (OSError, zipfile.BadZipFile):
        return False


def extract_file(source: Path, destination: Path, *, budget_seconds: float = 120,
                 local_whisper_model: Path | None = None) -> ExtractedSource:
    """Decode one real file into a new private local package, without overwrite.

    Unusable/malformed/unsupported input yields a HELD envelope. Timeout/resource
    exhaustion raises a fixed error so the caller can retry a future cycle. No
    speech model is downloaded: transcription requires an explicit local model.
    The caller owns durable storage, coordination and retention of artifacts.
    """
    if (not isinstance(budget_seconds, (int, float)) or isinstance(budget_seconds, bool)
            or not math.isfinite(budget_seconds) or not 0 < budget_seconds <= 900):
        raise ExtractionError("invalid_extraction_budget")
    deadline = time.monotonic() + budget_seconds
    source, destination = _safe_absolute(source), _safe_absolute(destination)
    if destination.exists():
        raise ExtractionError("destination_exists")
    try:
        handle = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                         getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise ExtractionError("source_unavailable") from None
    with os.fdopen(handle, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ExtractionError("source_not_regular")
        if info.st_size > MAX_INPUT_BYTES:
            raise ExtractionError("source_budget_exceeded")
        raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ExtractionError("source_budget_exceeded")
        after = os.fstat(stream.fileno())
        if (len(raw) != info.st_size or
                (info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev) !=
                (after.st_size, after.st_mtime_ns, after.st_ino, after.st_dev)):
            raise ExtractionError("source_changed_during_read")
    if time.monotonic() >= deadline:
        raise ExtractionError("extraction_deadline")
    sha = hashlib.sha256(raw).hexdigest()
    kind = _kind(source.suffix.lower())
    model: Path | None = None
    if local_whisper_model is not None:
        model = _safe_absolute(local_whisper_model)
        if not model.is_dir() or not (model / "model.bin").is_file():
            raise ExtractionError("local_speech_model_unavailable")
    try:
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    except OSError:
        raise ExtractionError("destination_unavailable") from None
    reason = "no_extracted_evidence"
    summary: dict[str, Any] | None = None
    with tempfile.TemporaryDirectory(prefix="tl-source-") as temporary:
        local = Path(temporary) / ("source" + source.suffix.lower())
        local.write_bytes(raw)
        arguments = [str(local), "--out", str(destination)]
        script: str | None = None
        if kind in {"text", "document"}:
            script = "text_document_ingest.py"
        elif kind == "spreadsheet":
            script = "spreadsheet_ingest.py"
        elif kind == "image":
            script = "image_ingest.py"
            arguments += ["--ocr", "--ocr-languages", "eng+por"]
        elif kind == "video":
            script = "ingest_video.py"
            arguments += ["--ocr", "--max-keyframes", "4"]
        elif kind == "audio":
            script = "audio_ingest.py"
        elif kind == "link":
            reason = "link_content_unverified"
        else:
            reason = "unsupported_format"
        if kind in {"audio", "video"} and model is not None:
            arguments += ["--transcribe", "--whisper-model", str(model)]
        if source.suffix.lower() in {".docx", ".pptx", ".xlsx", ".xlsm"} and not _archive_safe(local):
            script, reason = None, "invalid_or_oversize_archive"
        if script is not None:
            if _run(script, arguments, destination, deadline):
                if kind == "video":
                    if not _run("evidence_compactor.py", [str(destination)], destination, deadline):
                        raise ExtractionError("evidence_compaction_failed")
                path = destination / "evidence-summary.json"
                try:
                    if path.stat().st_size > MAX_SUMMARY_BYTES:
                        raise ExtractionError("summary_budget_exceeded")
                    summary = json.loads(path.read_bytes())
                except (OSError, ValueError):
                    raise ExtractionError("summary_invalid") from None
            else:
                reason = "decoder_failed"
    if summary is None:
        summary = {"schema_version": 1, "evidence": {}, "warnings": [reason]}
    semantic, errors = semantic_summary(summary)
    if errors or semantic is None:
        raise ExtractionError("summary_invalid")
    # Strip raw source/provider identifiers before constructing the envelope.
    summary = {"schema_version": 1, **semantic}
    if len(encode({"evidence": summary.get("evidence", {})})) > 3000:
        summary = {"schema_version": 1, "evidence": {},
                   "warnings": ["evidence_budget_exceeded"]}
    evidence_raw = encode(summary)
    if len(evidence_raw) > MAX_SUMMARY_BYTES:
        raise ExtractionError("summary_budget_exceeded")
    package_id = hashlib.sha256(("e1-source-v1|" + kind + "|" + sha).encode()).hexdigest()[:20]
    revision = hashlib.sha256(("e1-extraction-v1|" + sha).encode()).hexdigest()[:20]
    item = {"package_id": package_id, "revision_key": revision, "kind": kind,
            "evidence_path": f"99_INBOX/READY_FOR_ANALYSIS/{package_id}/evidence-summary.json",
            "detail_path": None, "preview_path": None}
    envelope, errors = build_envelope(item, evidence_raw)
    if errors or envelope is None:
        raise ExtractionError("envelope_invalid")
    # These local package files are private staging, never a Drive publication.
    (destination / "evidence-summary.json").write_bytes(evidence_raw)
    (destination / "source-provenance.json").write_bytes(encode({
        "schema_version": 1, "source_sha256": sha,
        "source_bytes": len(raw), "kind": kind,
        "evidence_sha256": hashlib.sha256(evidence_raw).hexdigest(),
        "assessment": assess_evidence(kind, summary),
    }))
    return ExtractedSource(package_id, revision, kind, sha, evidence_raw, envelope, destination)
