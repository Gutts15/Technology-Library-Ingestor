"""Daily selection through the real cycle CLI; external stage/storage ports isolated."""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import candidate_local_cycle as cycle


class DailyCandidateCycleTests(unittest.TestCase):
    def invoke(self, count, *, daily=True, max_batch=20, malformed=False, stale=False):
        ids = [f"{number:020x}" for number in range(1, count + 1)]
        now = datetime.now(timezone.utc).isoformat()
        index = {"schema_version": 1, "entries": [
            {"candidate_id": value, "valid": True, "captured_at": now} for value in ids
        ]}
        proposals = {"schema_version": 1, "entries": [
            {"candidate_id": value, "proposal": "READY_FOR_SEMANTIC"} for value in ids
        ]}
        decisions = {"schema_version": 1, "items": [
            {"candidate_id": value} for value in ([] if stale else ids[:max_batch])
        ]}

        def read_json(_remote, relative):
            if relative.endswith(cycle.DEFAULT_PROPOSALS):
                return proposals
            if relative.endswith(cycle.DEFAULT_DECISIONS):
                return decisions
            return None if malformed else index

        commands = []
        def execute(command):
            commands.append(command)
            return 0

        arguments = ["cycle", "--model", "synthetic-model", "--max-batch", str(max_batch)]
        if daily:
            # Daily selection must prevail over an inherited adaptive bulk threshold.
            arguments += ["--daily", "--volume-threshold", "500"]
        stream = io.StringIO()
        with patch.object(sys, "argv", arguments), \
             patch.object(cycle.shutil, "which", return_value="/synthetic/rclone"), \
             patch.object(cycle, "remote_json", side_effect=read_json), \
             patch.object(cycle, "remote_bytes", return_value=json.dumps(index).encode()), \
             patch.object(cycle, "run_command", side_effect=execute), redirect_stdout(stream):
            result = cycle.main()
        return result, commands, stream.getvalue()

    def test_single_fresh_candidate_runs_without_force(self):
        result, commands, output = self.invoke(1)
        self.assertEqual(result, 0)
        self.assertIn("selected=1", output)
        self.assertEqual([command[1] for command in commands], [
            "src/candidate_queue.py", "src/candidate_validator.py",
            "src/candidate_semantic_batch.py", "src/candidate_resolution.py",
            "src/candidate_finalize_local.py",
        ])
        self.assertNotIn("--force", commands[2])
        self.assertEqual(commands[2][commands[2].index("--volume-threshold") + 1], "1")
        self.assertIn("canonical_write=0", output)

    def test_empty_daily_cycle_does_not_start_semantic_work(self):
        result, commands, output = self.invoke(0)
        self.assertEqual(result, 0)
        self.assertEqual(len(commands), 2)
        self.assertIn("state=idle selected=0", output)

    def test_daily_batch_remains_bounded(self):
        result, commands, output = self.invoke(3, max_batch=2)
        self.assertEqual(result, 0)
        self.assertIn("selected=2", output)
        self.assertEqual(commands[-1][commands[-1].index("--max-items") + 1], "2")

    def test_invalid_state_cannot_start_semantic_work(self):
        result, commands, _output = self.invoke(1, malformed=True)
        self.assertEqual(result, 2)
        self.assertEqual(len(commands), 2)

    def test_stale_decisions_cannot_resolve_or_finalize(self):
        result, commands, output = self.invoke(1, stale=True)
        self.assertEqual(result, 2)
        self.assertEqual(len(commands), 3)
        self.assertIn("decision_batch_mismatch", output)

    def test_legacy_adaptive_cycle_is_preserved(self):
        result, commands, output = self.invoke(1, daily=False)
        self.assertEqual(result, 0)
        self.assertEqual(len(commands), 2)
        self.assertIn("state=idle selected=0", output)


if __name__ == "__main__":
    unittest.main()
