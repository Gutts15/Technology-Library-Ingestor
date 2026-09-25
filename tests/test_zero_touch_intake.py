from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import zero_touch_intake as intake


def item(number: int, *, size: int = 8, revision: str = "2026-01-01T00:00:00Z") -> dict:
    return {
        "ID": f"synthetic-id-{number}", "Name": f"synthetic-{number}.txt",
        "Path": f"synthetic-{number}.txt", "Size": size, "ModTime": revision,
        "MimeType": "text/plain", "Hashes": {"MD5": f"synthetic-hash-{number}-{revision}"},
    }


class FakeStore:
    def __init__(self, items: list[dict] | None = None):
        self.items = items or []
        self.states: dict[str, dict] = {}
        self.destinations: dict[str, dict] = {}
        self.copies = 0
        self.fail_after_copy = False

    def list_inbox(self) -> list[dict]:
        return copy.deepcopy(self.items)

    def read_state(self, key: str) -> dict:
        return copy.deepcopy(self.states.get(key, {"completed": [], "lease": None}))

    def write_state(self, key: str, state: dict) -> None:
        if self.fail_after_copy and self.copies:
            raise intake.IntakeError("synthetic_interruption")
        self.states[key] = copy.deepcopy(state)

    def ensure_destination_dir(self, destination: str) -> None:
        pass

    def stat(self, relative: str) -> dict | None:
        if relative.startswith(intake.INBOX + "/"):
            return next((copy.deepcopy(x) for x in self.items if relative.endswith("/" + x["Name"])), None)
        return copy.deepcopy(self.destinations.get(relative))

    def enqueue(self, source: str, destination: str) -> None:
        self.copies += 1
        original = self.stat(source)
        if original is None:
            raise intake.IntakeError("source_missing")
        self.destinations[destination] = original


class IntakeTests(unittest.TestCase):
    def test_idle_is_read_only(self) -> None:
        store = FakeStore()
        self.assertEqual(0, intake.process(store, now=100)["enqueued"])
        self.assertEqual({}, store.states)

    def test_new_item_and_already_processed_are_idempotent(self) -> None:
        store = FakeStore([item(1)])
        self.assertEqual(1, intake.process(store, now=100)["enqueued"])
        self.assertEqual(1, intake.process(store, now=200)["already_done"])
        self.assertEqual(1, store.copies)

    def test_multiple_items_and_batch_backpressure(self) -> None:
        store = FakeStore([item(i) for i in range(8)])
        first = intake.process(store, now=100)
        self.assertEqual(intake.MAX_ITEMS, first["enqueued"])
        self.assertEqual(3, first["deferred"])
        self.assertEqual(3, intake.process(store, now=200)["enqueued"])
        self.assertEqual(8, store.copies)

    def test_byte_limit_and_oversize_are_deferred(self) -> None:
        store = FakeStore([item(1, size=intake.MAX_ITEM_BYTES + 1)])
        self.assertEqual(1, intake.process(store, now=100)["deferred"])
        self.assertEqual(0, store.copies)

    def test_valid_lease_blocks_second_run_then_expires(self) -> None:
        source = item(1)
        store = FakeStore([source])
        key = intake.identity(source)
        rev = intake.revision(source)
        store.states[key] = {"completed": [], "lease": {"revision": rev, "until": 200}}
        self.assertEqual(1, intake.process(store, now=100)["leased"])
        self.assertEqual(1, intake.process(store, now=201)["enqueued"])
        self.assertEqual(1, store.copies)

    def test_interruption_after_copy_resumes_without_duplicate(self) -> None:
        store = FakeStore([item(1)])
        store.fail_after_copy = True
        with self.assertRaises(intake.IntakeError):
            intake.process(store, now=100)
        store.fail_after_copy = False
        self.assertEqual(1, intake.process(store, now=100 + intake.LEASE_SECONDS + 1)["enqueued"])
        self.assertEqual(1, store.copies)

    def test_revision_of_same_id_is_enqueued_once(self) -> None:
        store = FakeStore([item(1)])
        intake.process(store, now=100)
        store.items = [item(1, revision="2026-01-02T00:00:00Z")]
        self.assertEqual(1, intake.process(store, now=200)["enqueued"])
        self.assertEqual(1, intake.process(store, now=300)["already_done"])
        self.assertEqual(2, store.copies)
        self.assertEqual(1, len(store.states))

    def test_baseline_skips_existing_without_copy(self) -> None:
        store = FakeStore([item(1)])
        self.assertEqual(1, intake.process(store, baseline=True, now=100)["baselined"])
        self.assertEqual(0, store.copies)
        self.assertEqual(1, intake.process(store, now=200)["already_done"])
        store.items.append(item(2))
        self.assertEqual(1, intake.process(store, now=300)["enqueued"])

    def test_missing_provider_id_is_not_enqueued(self) -> None:
        source = item(1)
        del source["ID"]
        store = FakeStore([source])
        self.assertEqual(1, intake.process(store, now=100)["unsupported"])
        self.assertEqual(0, store.copies)

    def test_source_changed_during_copy_does_not_mark_done(self) -> None:
        store = FakeStore([item(1)])
        original_enqueue = store.enqueue

        def changing_enqueue(source: str, destination: str) -> None:
            original_enqueue(source, destination)
            store.items = [item(1, revision="2026-01-02T00:00:00Z")]

        store.enqueue = changing_enqueue
        with self.assertRaisesRegex(intake.IntakeError, "source_changed"):
            intake.process(store, now=100)
        state = next(iter(store.states.values()))
        self.assertEqual([], state["completed"])


if __name__ == "__main__":
    unittest.main()
