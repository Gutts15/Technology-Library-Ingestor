#!/usr/bin/env python3
"""Regression checks for rclone remote-root path joining."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_curation_prepare import join_remote as curation_join_remote
from candidate_finalize_local import join_remote as finalize_join_remote
from candidate_local_cycle import join_remote as cycle_join_remote
from candidate_queue import join_remote as queue_join_remote
from candidate_resolution import join_remote as resolution_join_remote
from candidate_semantic_batch import join_remote as semantic_join_remote
from file_evidence_candidate_bridge import Storage
from rclone_paths import join_remote
from run_report import upload_report


CASES = (
    ("tl:", "99_INBOX/x", "tl:99_INBOX/x"),
    ("tl:base", "x", "tl:base/x"),
    ("tl:base/", "/x", "tl:base/x"),
    ("tl:", "", "tl:"),
)


def main() -> None:
    implementations = (
        queue_join_remote,
        cycle_join_remote,
        semantic_join_remote,
        resolution_join_remote,
        finalize_join_remote,
        curation_join_remote,
    )
    for implementation in implementations:
        assert implementation is join_remote
        for remote, relative, expected in CASES:
            actual = implementation(remote, relative)
            assert actual == expected, (
                f"{implementation.__module__}.join_remote({remote!r}, {relative!r}) "
                f"returned {actual!r}; expected {expected!r}"
            )

    storage = Storage(remote="tl:")
    assert storage.target("99_INBOX/x") == "tl:99_INBOX/x"

    rclone_calls: list[list[str]] = []

    def fake_rclone(arguments: list[str]) -> SimpleNamespace:
        rclone_calls.append(arguments)
        return SimpleNamespace(returncode=0)

    with patch("run_report.shutil.which", return_value="rclone"), patch(
        "run_report.run_rclone", side_effect=fake_rclone
    ):
        assert upload_report("tl:", "99_INBOX/reports", Path("report.json"), "run", 1)
    assert rclone_calls[0][1] == "tl:99_INBOX/reports"
    assert rclone_calls[1][2] == "tl:99_INBOX/reports/run-run-attempt-1.json"
    print("rclone_path_regression_ok")


if __name__ == "__main__":
    main()
