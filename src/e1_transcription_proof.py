#!/usr/bin/env python3
"""Proof: a spoken synthetic video yields transcript evidence in the cloud.

Generates speech with espeak-ng, muxes it into a short video with ffmpeg,
runs the real E1 extractor with the local faster-whisper model and checks
that spoken words reach the evidence summary. Synthetic content only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e1_source_extract import extract_file  # noqa: E402

SPEECH = ("Pixel Rig Tool is an open source library for skeletal rigging of two "
          "dimensional sprites in web games.")


def main() -> int:
    model = os.environ.get("TL_WHISPER_MODEL")
    if not model:
        print("e1_transcription_proof status=MODEL_MISSING")
        return 2
    with tempfile.TemporaryDirectory(prefix="tl-tts-") as temp:
        t = Path(temp)
        subprocess.run(["espeak-ng", "-s", "140", "-w", str(t / "s.wav"), SPEECH], check=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=320x240:d=9",
                        "-i", str(t / "s.wav"), "-shortest", "-c:v", "libx264", "-c:a", "aac",
                        str(t / "clip.mp4")], check=True)
        got = extract_file(t / "clip.mp4", t / "out", budget_seconds=600,
                           local_whisper_model=Path(model))
        text = json.dumps(json.loads(got.evidence_raw), ensure_ascii=False).lower()
    hits = [w for w in ("pixel", "rig", "sprite", "skeletal") if w in text]
    print(f"e1_transcription_proof kind={got.kind} words_found={len(hits)}/4 "
          f"state={got.assessment['state']} evidence_bytes={len(got.evidence_raw)}")
    ok = got.kind == "video" and len(hits) >= 2
    print("e1_transcription_proof status=" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
