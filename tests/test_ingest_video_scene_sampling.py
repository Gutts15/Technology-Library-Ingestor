"""Synthetic scene-selection regressions for Stage 21A."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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

    def test_video_without_decodable_frames_records_weak_visual_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "synthetic.mp4"
            source.write_bytes(b"synthetic test bytes")
            output = root / "out"
            probe = {
                "format": {"duration": "1.0"},
                "streams": [{"codec_type": "video", "width": 320, "height": 240}],
            }
            stats = {"selected_keyframes": 0}
            argv = ["ingest_video.py", str(source), "--out", str(output)]
            log = io.StringIO()

            with patch.object(sys, "argv", argv), \
                 patch.object(ingest_video, "ffprobe", return_value=probe), \
                 patch.object(ingest_video, "build_keyframes", return_value=([], stats)), \
                 redirect_stdout(log):
                self.assertEqual(ingest_video.main(), 0)

            manifest = json.loads((output / "ingest.json").read_text(encoding="utf-8"))
            self.assertIn("no_decodable_keyframes", manifest["processing"]["warnings"])
            self.assertNotIn(source.name, log.getvalue())


if __name__ == "__main__":
    unittest.main()
