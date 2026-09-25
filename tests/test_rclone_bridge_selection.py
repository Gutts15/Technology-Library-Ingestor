#!/usr/bin/env python3
"""Deterministic exact-selection check for the video bridge."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rclone_bridge import select_pending


def main() -> None:
    items = [
        {"Name": "a.mp4", "ModTime": "2026-09-20T00:00:00Z"},
        {"Name": "b.mp4", "ModTime": "2026-09-20T00:00:01Z"},
    ]
    selected, error = select_pending(items, "b.mp4", 1)
    assert error is None and selected == [items[1]]
    assert select_pending(items, "missing.mp4", 1) == ([], "selected_item_missing")
    selected, error = select_pending(items, None, 1)
    assert error is None and selected == [items[0]]
    print("rclone_bridge_selection_smoke_ok exact=1 bounded=1")


if __name__ == "__main__":
    main()
