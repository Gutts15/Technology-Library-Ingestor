"""Real FFmpeg smoke for Stage 21A using a generated, private-data-free video."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import ingest_video


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools unavailable")
class FfmpegIntegrationTests(unittest.TestCase):
    def test_late_visual_change_survives_real_decode_and_stays_out_of_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "synthetic_source.mp4"
            output = root / "output"
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=4:duration=3",
                "-f", "lavfi", "-i", "smptebars=size=320x180:rate=4:duration=3",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
                "-map", "[v]", "-c:v", "mpeg4", "-pix_fmt", "yuv420p", str(source),
            ]
            subprocess.run(command, check=True, capture_output=True)

            log = io.StringIO()
            argv = ["ingest_video.py", str(source), "--out", str(output), "--max-keyframes", "8"]
            with patch.object(sys, "argv", argv), redirect_stdout(log):
                self.assertEqual(ingest_video.main(), 0)

            manifest = json.loads((output / "ingest.json").read_text(encoding="utf-8"))
            keyframes = manifest["artifacts"]["keyframes"]
            self.assertEqual(manifest["processing"]["status"], "processed")
            self.assertGreaterEqual(manifest["processing"]["keyframe_stats"]["scene_candidates"], 1)
            self.assertTrue(any(frame["timestamp_seconds"] >= 3.0 for frame in keyframes))
            self.assertNotIn("no_decodable_keyframes", manifest["processing"]["warnings"])
            self.assertNotIn(source.name, log.getvalue())
            self.assertNotIn(str(root), log.getvalue())


if __name__ == "__main__":
    unittest.main()
