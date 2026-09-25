#!/usr/bin/env python3
"""Deterministic selection and receipt routing for the opt-in real item proof."""

from __future__ import annotations

import json
import io
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stage16b_real_item import (
    bridge_result, decode_small_input, main as cli_main, package_from_summary,
    run, run_step, select_exact_input, select_small_input, video_dependency_error,
)


def main() -> None:
    items = [
        {"Name": "large.mp4", "Size": 1000000},
        {"Name": "real-note.txt", "Size": 80},
        {"Name": "real-shortcut.url", "Size": 120},
    ]
    chosen, error = select_small_input(items)
    assert error is None and chosen is items[2]
    assert select_small_input([items[0]]) == (None, "small_link_or_text_missing")
    video = {"Name": "proof.mp4", "Size": 5_900_000, "MimeType": "video/mp4"}
    assert select_exact_input([video], "proof.mp4") == (video, None)
    assert select_exact_input([video], "missing.mp4") == (None, "selected_item_missing")
    with patch("stage16b_real_item.shutil.which", side_effect=lambda name: None if name == "ffmpeg" else "ok"):
        assert video_dependency_error() == "ffmpeg_missing"
    with patch("stage16b_real_item.shutil.which", side_effect=lambda name: None if name == "ffprobe" else "ok"):
        assert video_dependency_error() == "ffprobe_missing"
    assert "https://example.com/tool" in decode_small_input("ação https://example.com/tool".encode("cp1252"))
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        (workspace / "bridge-summary.json").write_text(json.dumps({
            "selected": 1, "processed": 1, "finalized": 0, "errors": 0, "packages": ["a" * 20]
        }), encoding="utf-8")
        assert package_from_summary(workspace, "link") == "a" * 20
        (workspace / "content-bridge-summary.json").write_text(json.dumps({
            "selected": 1, "errors": 0, "results": [{"package_id": "b" * 20, "status": "processed"}]
        }), encoding="utf-8")
        assert package_from_summary(workspace, "text") == "b" * 20
        (workspace / "bridge-summary.json").write_text(json.dumps({
            "schema_version": 1, "selected": 1, "processed": 1, "errors": 0,
            "results": [{"package_id": "c" * 20, "status": "processed"}]
        }), encoding="utf-8")
        assert package_from_summary(workspace, "video") == "c" * 20
        (workspace / "bridge-summary.json").write_text(json.dumps({
            "selected": 1, "processed": 0, "finalized": 0, "errors": 0, "packages": ["a" * 20]
        }), encoding="utf-8")
        assert package_from_summary(workspace, "link") is None
    def exercise_bridge(outcome: str, reason: str) -> tuple[tuple[str, str], list[list[str]], str]:
        commands: list[list[str]] = []
        def record_command(command: list[str], _stage: str) -> bool:
            commands.append(command)
            if "file_evidence_candidate_bridge.py" in command[1]:
                report = Path(command[command.index("--report") + 1])
                report.write_text(json.dumps({"schema_version": 1, "canonical_write": 0,
                                              "outcome": outcome, "reason_code": reason}), encoding="utf-8")
                return outcome != "held"
            return True
        output = io.StringIO()
        with patch("stage16b_real_item.list_items", return_value=[items[2]]), \
             patch("stage16b_real_item.run_rclone", return_value=SimpleNamespace(returncode=0)), \
             patch("stage16b_real_item.remote_exists", return_value=False), \
             patch("stage16b_real_item.move_without_overwrite", return_value="moved"), \
             patch("stage16b_real_item.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=b"https://example.com/tool")), \
             patch("stage16b_real_item.run_step", side_effect=record_command), \
             patch("stage16b_real_item.package_from_summary", return_value="a" * 20), \
             redirect_stdout(output):
            result = run("tl:")
        return result, commands, output.getvalue()
    result, commands, progress = exercise_bridge("created", "candidate_created")
    assert result == ("created", "stage16b_complete")
    assert len(commands) == 5
    assert "--only-name" in commands[0]
    assert "real-shortcut.url" in commands[0]
    assert commands[-1][commands[-1].index("--package-id") + 1] == "a" * 20
    assert "stage=file_evidence" in progress and "stage=candidate_bridge" in progress
    assert "real-shortcut.url" not in progress
    assert exercise_bridge("held", "no_reusable_candidate")[0] == ("held", "no_reusable_candidate")

    video_commands: list[list[str]] = []
    def video_command(command: list[str], _stage: str) -> bool:
        video_commands.append(command)
        if "file_evidence_candidate_bridge.py" in command[1]:
            report = Path(command[command.index("--report") + 1])
            report.write_text(json.dumps({"schema_version": 1, "canonical_write": 0,
                                          "outcome": "created", "reason_code": "candidate_created"}), encoding="utf-8")
        return True
    with patch("stage16b_real_item.list_items", return_value=[video]), \
         patch("stage16b_real_item.run_rclone", return_value=SimpleNamespace(returncode=0)), \
         patch("stage16b_real_item.remote_exists", return_value=False), \
         patch("stage16b_real_item.move_without_overwrite", return_value="moved"), \
         patch("stage16b_real_item.run_step", side_effect=video_command), \
         patch("stage16b_real_item.package_from_summary", return_value="c" * 20), \
         patch("stage16b_real_item.shutil.which", return_value="/usr/bin/tool"):
        assert run("tl:", only_name="proof.mp4", source_url="https://example.com/public-tool") == ("created", "stage16b_complete")
    assert "rclone_bridge.py" in video_commands[0][1]
    assert "--only-name" in video_commands[0] and "proof.mp4" in video_commands[0]
    assert "--transcribe" in video_commands[0] and "--ocr" in video_commands[0]
    assert "--source-url" in video_commands[-1]
    with patch("stage16b_real_item.subprocess.run", side_effect=subprocess.TimeoutExpired(["test"], 1800)):
        assert run_step(["test"], "candidate_bridge") is False
    with tempfile.TemporaryDirectory() as temporary:
        invalid = Path(temporary) / "invalid.json"
        invalid.write_text(json.dumps({"schema_version": 1, "canonical_write": 0,
                                      "outcome": "held", "reason_code": "private value"}), encoding="utf-8")
        assert bridge_result(invalid) is None
    with tempfile.TemporaryDirectory() as temporary:
        report = Path(temporary) / "sanitized.json"
        with patch.object(sys, "argv", ["stage16b_real_item.py", "--remote", "tl:", "--report", str(report)]), \
             patch("stage16b_real_item.shutil.which", return_value=None):
            assert cli_main() == 2
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload == {"schema_version": 1, "stage": "16A-16B", "outcome": "held",
                           "reason_code": "rclone_missing", "canonical_write": 0, "paid_model": 0}
    print("stage16b_real_item_smoke_ok selected=1 package_bound=1")


if __name__ == "__main__":
    main()
