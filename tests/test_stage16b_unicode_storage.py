#!/usr/bin/env python3
"""Regression: rclone UTF-8 JSON must not use the Windows cp1252 locale."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import curation_handoff
import link_bridge
import media_router
import ready_index
import text_document_bridge


def main() -> None:
    name = "ação técnica.url"
    payload = json.dumps([{"Name": name, "Path": name, "Size": 90}], ensure_ascii=False).encode("utf-8")
    result = subprocess.CompletedProcess(["rclone"], 0, stdout=payload, stderr=b"")

    with patch("media_router.run_rclone", return_value=result):
        items = media_router.list_items("tl:", "99_INBOX/DROP_HERE")
        assert items is not None and items[0]["Name"] == name
    with patch("link_bridge.run_rclone", return_value=result):
        items = link_bridge.list_supported("tl:", "99_INBOX/TO_REVIEW/LINKS")
        assert items is not None and items[0]["Name"] == name

    text_name = "informação técnica.txt"
    text_payload = json.dumps([{"Name": text_name, "Path": text_name, "Size": 90}], ensure_ascii=False).encode("utf-8")
    text_result = subprocess.CompletedProcess(["rclone"], 0, stdout=text_payload, stderr=b"")
    with patch("text_document_bridge.run_rclone", return_value=text_result):
        items = text_document_bridge.list_supported("tl:", "99_INBOX/TO_REVIEW/TEXT", {".txt"}, "text")
        assert items is not None and items[0]["Name"] == text_name

    invalid = subprocess.CompletedProcess(["rclone"], 0, stdout=b"\x8f", stderr=b"")
    with patch("media_router.run_rclone", return_value=invalid):
        assert media_router.list_items("tl:", "99_INBOX/DROP_HERE") is None

    for module in (media_router, link_bridge, text_document_bridge):
        with patch.object(module.subprocess, "run", return_value=result) as call:
            module.run_rclone(["lsjson", "tl:somewhere"])
            assert call.call_args.kwargs.get("text") is not True
    for module in (ready_index, curation_handoff):
        with patch.object(module.subprocess, "run", return_value=result) as call:
            module.run_rclone(["lsjson", "tl:somewhere"])
            assert call.call_args.kwargs["encoding"] == "utf-8"

    print("stage16b_unicode_storage_smoke_ok utf8_names=3 malformed_hold=1 locale_independent=1")


if __name__ == "__main__":
    main()
