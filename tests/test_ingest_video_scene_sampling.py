"""Synthetic scene-selection regressions for Stage 21A."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import ingest_video


class SceneSamplingTests(unittest.TestCase):
    def test_dense_scenes_cover_entire_video_with_bounded_candidates(self) -> None:
        stderr = "\n".join(f"pts_time:{index * 2}.0" for index in range(120))
        result = SimpleNamespace(returncode=0, stderr=stderr)

        with patch.object(ingest_video.subprocess, "run", return_value=result):
            timestamps = ingest_video.detect_scene_timestamps(Path("synthetic.mp4"), 0.30)

        self.assertEqual(len(timestamps), ingest_video.MAX_SCENE_CANDIDATES)
        self.assertEqual(timestamps[0], 0.0)
        self.assertEqual(timestamps[-1], 238.0)
        self.assertEqual(timestamps, sorted(set(timestamps)))

    def test_few_scenes_are_not_resampled(self) -> None:
        result = SimpleNamespace(returncode=0, stderr="pts_time:1.0\npts_time:3.0\npts_time:5.0")

        with patch.object(ingest_video.subprocess, "run", return_value=result):
            timestamps = ingest_video.detect_scene_timestamps(Path("synthetic.mp4"), 0.30)

        self.assertEqual(timestamps, [1.0, 3.0, 5.0])


if __name__ == "__main__":
    unittest.main()
