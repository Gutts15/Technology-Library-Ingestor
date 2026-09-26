from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tests.test_zero_touch_intake import FakeStore, item
from intake_daily_session import run_session


class SessionStore(FakeStore):
    def __init__(self, items: list[dict] | None = None):
        super().__init__(items)
        self.session: dict | None = None
        self.list_calls = 0

    def list_inbox(self) -> list[dict]:
        self.list_calls += 1
        return super().list_inbox()

    def read_session(self) -> dict | None:
        return copy.deepcopy(self.session)

    def write_session(self, value: dict) -> None:
        self.session = copy.deepcopy(value)

    def baseline_complete(self) -> bool:
        return True


class DailySessionTests(unittest.TestCase):
    def test_exact_worker_counts_for_requested_backlogs(self) -> None:
        for count, expected_runs in ((0, 1), (3, 1), (5, 1), (8, 2), (12, 3), (30, 6)):
            with self.subTest(count=count):
                store = SessionStore([item(i) for i in range(count)])
                runs = 0
                continuation = False
                while True:
                    result = run_session(
                        store, continuation=continuation, fixture_only=False,
                        now=100 + runs, cycle="2026-09-26",
                    )
                    runs += 1
                    if not result["continue"]:
                        break
                    continuation = True
                    self.assertLess(runs, 25)
                self.assertEqual(expected_runs, runs)
                self.assertEqual(count, store.copies)
                self.assertEqual("closed", store.session["status"])

    def test_session_freezes_eligible_snapshot(self) -> None:
        store = SessionStore([item(i) for i in range(8)])
        first = run_session(store, continuation=False, fixture_only=False, now=100, cycle="2026-09-26")
        self.assertTrue(first["continue"])
        store.items.append(item(8))
        second = run_session(store, continuation=True, fixture_only=False, now=101, cycle="2026-09-26")
        self.assertFalse(second["continue"])
        self.assertEqual(8, store.copies)
        self.assertEqual(8, len(store.destinations))
        third = run_session(store, continuation=False, fixture_only=False, now=24 * 3600 + 100, cycle="2026-09-27")
        self.assertFalse(third["continue"])
        self.assertEqual(9, store.copies)

    def test_closed_session_does_not_poll_again_same_day(self) -> None:
        store = SessionStore()
        result = run_session(store, continuation=False, fixture_only=False, now=100, cycle="2026-09-26")
        self.assertFalse(result["continue"])
        self.assertEqual(1, store.list_calls)
        run_session(store, continuation=True, fixture_only=False, now=101, cycle="2026-09-26")
        run_session(store, continuation=False, fixture_only=False, now=102, cycle="2026-09-26")
        self.assertEqual(1, store.list_calls)

    def test_fixture_only_session_never_processes_real_name(self) -> None:
        synthetic, real = item(1), item(2)
        synthetic["Name"] = synthetic["Path"] = "STAGE20_SYNTHETIC_20260925_Z.txt"
        store = SessionStore([synthetic, real])
        result = run_session(store, continuation=False, fixture_only=True, now=100, cycle="2026-09-26")
        self.assertFalse(result["continue"])
        self.assertEqual(1, store.copies)
        self.assertEqual(1, len(store.destinations))

    def test_interruption_resumes_next_day_without_duplicate_copy(self) -> None:
        store = SessionStore([item(1)])
        store.fail_after_copy = True
        with self.assertRaisesRegex(Exception, "synthetic_interruption"):
            run_session(store, continuation=False, fixture_only=False, now=100, cycle="2026-09-26")
        self.assertEqual(1, store.copies)
        store.fail_after_copy = False
        resumed = run_session(
            store, continuation=False, fixture_only=False,
            now=24 * 3600 + 100, cycle="2026-09-27",
        )
        self.assertFalse(resumed["continue"])
        self.assertEqual(1, store.copies)

    def test_exceptional_run_ceiling_rolls_backlog_to_next_day(self) -> None:
        store = SessionStore([item(i) for i in range(125)])
        runs = 0
        continuation = False
        while True:
            result = run_session(
                store, continuation=continuation, fixture_only=False,
                now=100 + runs, cycle="2026-09-26",
            )
            runs += 1
            if not result["continue"]:
                break
            continuation = True
        self.assertEqual(24, runs)
        self.assertEqual(120, store.copies)
        self.assertEqual("ceiling", store.session["reason"])
        next_day = run_session(
            store, continuation=False, fixture_only=False,
            now=24 * 3600 + 100, cycle="2026-09-27",
        )
        self.assertFalse(next_day["continue"])
        self.assertEqual(125, store.copies)


if __name__ == "__main__":
    unittest.main()
