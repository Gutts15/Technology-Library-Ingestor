#!/usr/bin/env python3
"""Dependency-free smoke test for UTF-8 candidate queue decoding."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_queue import run_rclone


def main() -> None:
    sample = "automação integração não – UTF-8\n"
    completed = subprocess.CompletedProcess(
        args=["rclone", "cat", "tl:sample"],
        returncode=0,
        stdout=sample,
        stderr="",
    )

    with patch("candidate_queue.subprocess.run", return_value=completed) as mocked:
        result = run_rclone(["cat", "tl:sample"])

    kwargs = mocked.call_args.kwargs
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "strict"
    assert result.stdout == sample
    assert sample.encode("utf-8").decode("utf-8") == sample
    assert sample.encode("utf-8").decode("cp1252") != sample

    print("candidate_queue_encoding_smoke_ok utf8=1 strict=1 non_ascii=1")


if __name__ == "__main__":
    main()
