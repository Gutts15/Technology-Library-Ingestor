"""Offline Git exact-ref CAS ownership + E3 synthetic recovery integration tests."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from candidate_production_coordinator import acquire, read_state
from e3_coordinator_fixture import (
    CoordinatorFixtureError, lock_identity, resume_fixture, run_fixture,
)
from e3_recovery_fixture import (
    MARKER, MARKER_BYTES, load_journal,
)
from tests.test_e3_transaction_preflight import E3PreflightTests
from e3_transaction_preflight import compile_transaction


class CoordinatorRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixture = E3PreflightTests("test_no_write_decision_does_not_touch_canonical")
        fixture.setUp()
        self.addCleanup(fixture.tmp.cleanup)
        self.fixture = fixture
        self.root = fixture.root
        fixture.write(MARKER, MARKER_BYTES)
        self.git = self.root / "coord.git"
        subprocess.run(["git", "init", "--bare", str(self.git)], check=True, capture_output=True)
        self.raw = fixture.lote_raw
        self.relative = fixture.path
        self.decisions = fixture.decision_bytes(fixture.create_row())
        plan, _, _, s = compile_transaction(self.root, self.raw, self.relative, self.decisions)
        self.tx = plan["transaction_id"]
        self.batch = s["lote_id"]
        self.new_file = self.root / "00_LIBRARY/02_WEB_DEVELOPMENT/UI_COMPONENTS/technology-pixel-rig-tool.md"
        self.master = self.root / "00_LIBRARY/MASTER_INDEX.md"
        self.master_initial = self.master.read_bytes()

    def state(self):
        _, state, errors = read_state(str(self.git))
        self.assertFalse(errors)
        return state

    def test_successful_recovery_releases_exact_owner(self):
        state, tx = run_fixture(self.root, self.git, self.raw, self.relative, self.decisions)
        self.assertEqual((state, tx), ("RECOVERED_RELEASED", self.tx))
        self.assertEqual(self.master.read_bytes(), self.master_initial)
        self.assertFalse(self.new_file.exists())
        state = self.state()
        self.assertEqual(state["state"], "FREE")
        self.assertEqual(state["release_reason"], "VERIFIED_RECOVERED")
        self.assertEqual(state["released_transaction_id"], self.tx)

    def test_partial_write_keeps_ownership_until_next_session_recovers(self):
        state, tx = run_fixture(self.root, self.git, self.raw, self.relative, self.decisions,
                                fail_on_write=2)
        self.assertEqual((state, tx), ("RECOVERY_REQUIRED_OWNED", self.tx))
        self.assertTrue(self.new_file.exists())
        busy = self.state()
        self.assertEqual(busy["state"], "ACTIVE")
        self.assertTrue(busy["canonical_write_performed"])
        self.assertEqual(resume_fixture(self.root, self.git, self.tx, self.batch), "RECOVERED_RELEASED")
        self.assertFalse(self.new_file.exists())
        self.assertEqual(self.master.read_bytes(), self.master_initial)
        self.assertEqual(self.state()["state"], "FREE")

    def test_other_transaction_cannot_take_active_lock(self):
        run_fixture(self.root, self.git, self.raw, self.relative, self.decisions,
                    fail_on_write=1)
        active_before = self.state()
        _, _, err = acquire(
            str(self.git), transaction_id="f" * 20, batch_id="different",
            owner="candidate-publisher-" + "f" * 12,
            journal_path="99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/" + "f" * 20 +
                         "/candidate-publisher-" + "f" * 12 + "/journal.json")
        self.assertIn("coordinator_busy", err)
        self.assertEqual(self.state(), active_before)

    def test_foreign_canonical_edit_blocks_release(self):
        run_fixture(self.root, self.git, self.raw, self.relative, self.decisions,
                    fail_on_write=2)
        self.new_file.write_bytes(b"OTHER WRITER")
        with self.assertRaisesRegex(Exception, "recovery_external_conflict"):
            resume_fixture(self.root, self.git, self.tx, self.batch)
        self.assertEqual(self.state()["state"], "ACTIVE")
        self.assertEqual(self.new_file.read_bytes(), b"OTHER WRITER")

    def test_journal_tampering_blocks_coordinator_release(self):
        run_fixture(self.root, self.git, self.raw, self.relative, self.decisions,
                    fail_on_write=1)
        journal_path = (self.root / "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY" /
                        self.tx / ("candidate-publisher-" + "0" * 12) / "journal.json")
        journal_path.write_bytes(b"not valid json")
        with self.assertRaises(Exception):
            resume_fixture(self.root, self.git, self.tx, self.batch)
        self.assertEqual(self.state()["state"], "ACTIVE")

    def test_wrong_owner_and_batch_do_not_resume(self):
        run_fixture(self.root, self.git, self.raw, self.relative, self.decisions,
                    fail_on_write=1)
        with self.assertRaisesRegex(CoordinatorFixtureError, "coordinator_owner_mismatch"):
            resume_fixture(self.root, self.git, self.tx, "wrong")
        self.assertEqual(self.state()["state"], "ACTIVE")

    def test_remote_repo_must_be_local_bare_inside_fixture(self):
        other = Path("/tmp/remote-not-allowed.git")
        with self.assertRaisesRegex(CoordinatorFixtureError, "non_fixture_git_remote"):
            run_fixture(self.root, other, self.raw, self.relative, self.decisions)
        self.assertFalse(self.new_file.exists())

    def test_acquire_push_lost_ack_never_mutates_records(self):
        from unittest.mock import patch
        import candidate_production_coordinator as coord
        actual_push = coord._push_expected
        def send_but_lose_ack(work, sha):
            self.assertTrue(actual_push(work, sha))
            return False
        with patch.object(coord, "_push_expected", side_effect=send_but_lose_ack):
            with self.assertRaisesRegex(CoordinatorFixtureError, "coordinator_acquire_failed"):
                run_fixture(self.root, self.git, self.raw, self.relative, self.decisions)
        observed = self.state()
        self.assertEqual(observed["state"], "ACTIVE")
        self.assertFalse(observed["canonical_write_performed"])
        self.assertFalse(self.new_file.exists())
        self.assertEqual(self.master.read_bytes(), self.master_initial)

    def test_write_boundary_push_lost_ack_fails_closed(self):
        from unittest.mock import patch
        import candidate_production_coordinator as coord
        actual_push = coord._push_expected
        count = 0
        def send_with_second_ack_lost(work, sha):
            nonlocal count
            count += 1
            result = actual_push(work, sha)
            return result if count == 1 else False
        with patch.object(coord, "_push_expected", side_effect=send_with_second_ack_lost):
            with self.assertRaisesRegex(CoordinatorFixtureError, "coordinator_start_uncertain"):
                run_fixture(self.root, self.git, self.raw, self.relative, self.decisions)
        self.assertEqual(count, 2)
        self.assertTrue(self.state()["canonical_write_performed"])
        self.assertFalse(self.new_file.exists())
        journal = load_journal(self.root, self.tx)
        self.assertEqual(journal["state"], "PREPARED")
        with self.assertRaisesRegex(CoordinatorFixtureError, "uncertain_prewrite_boundary"):
            resume_fixture(self.root, self.git, self.tx, self.batch)
        self.assertEqual(self.state()["state"], "ACTIVE")

    def test_git_timeout_is_bounded_and_not_treated_as_success(self):
        from unittest.mock import patch
        from candidate_production_coordinator import git
        with patch("candidate_production_coordinator.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(["git", "ls-remote"], 40)) as proc:
            outcome = git(["ls-remote", str(self.git)])
        self.assertEqual(outcome.returncode, 124)
        self.assertEqual(proc.call_args.kwargs["timeout"], 40)

    def test_nonwrite_does_not_acquire_coordinator(self):
        neutral = self.fixture.decision_bytes(self.fixture.decision())
        result, tx = run_fixture(self.root, self.git, self.raw, self.relative, neutral)
        self.assertEqual((result, tx), ("NO_WRITE_DECISIONS", None))
        _, state, errors = read_state(str(self.git))
        self.assertEqual(errors, ["coordinator_ref_missing"])
        self.assertIsNone(state)


if __name__ == "__main__":
    unittest.main()
