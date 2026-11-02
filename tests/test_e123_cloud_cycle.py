"""Marco 3: full E1->E2->E3 cloud cycle over a fake private Drive.

The fake Drive is a plain directory; the model is a deterministic stand-in
for Gemini so CREATE / UPDATE / UNCHANGED / REJECT are all exercised through
the real engines, across separate cycles with fresh mirrors (as on runners).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import candidate_production_coordinator as coord  # noqa: E402
import e123_cloud_cycle as cycle  # noqa: E402
from e2_gemini_decide import expected_record_id  # noqa: E402
from library_index_build import planned_indexes, validate_record  # noqa: E402

COORD = "drive-lease:tl:" + cycle.LEASE_DIR
CREATED = "00_LIBRARY/02_WEB_DEVELOPMENT/UI_COMPONENTS/technology-pixel-rig-tool.md"
PRIOR = "00_LIBRARY/02_WEB_DEVELOPMENT/UI_COMPONENTS/technology-prior-tool.md"


class DirRemote:
    """Directory standing in for tl:. Also serves the coordinator lease."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.fail_writes_after: int | None = None
        self.writes = 0

    def _p(self, rel: str) -> Path:
        return self.base / rel.removeprefix("tl:")

    def pull(self, root: Path) -> None:
        prefixes = [r[3:].rstrip("*").rstrip("/") for r in cycle.MIRROR_FILTER if r.startswith("+ /")]
        for f in self.base.rglob("*"):
            rel = f.relative_to(self.base).as_posix()
            ok = any(rel.startswith(p + "/") for p in prefixes if "*" not in p) or (
                rel.startswith("99_INBOX/READY_FOR_ANALYSIS/") and rel.endswith("/evidence-summary.json"))
            if f.is_file() and ok:
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, root / rel)

    def list_sources(self):
        drop = self.base / cycle.DROP
        return [{"path": f.relative_to(drop).as_posix(), "size": f.stat().st_size,
                 "sha256": hashlib.sha256(f.read_bytes()).hexdigest(), "mtime": f.name}
                for f in sorted(drop.rglob("*")) if f.is_file()]

    def download(self, rel: str, dest: Path) -> None:
        shutil.copyfile(self.base / cycle.DROP / rel, dest)

    def read(self, rel: str):
        p = self._p(rel)
        return p.read_bytes() if p.is_file() else False

    def write(self, rel: str, raw: bytes) -> bool:
        self.writes += 1
        if self.fail_writes_after is not None and self.writes > self.fail_writes_after:
            return False
        p = self._p(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
        return True


def fake_gemini(drive: DirRemote):
    def ask(item, api_key, timeout=60):
        text = json.dumps(item["evidence_compact"])
        base = {"item_id": item["item_id"], "motivo": None, "confianca": 0.95,
                "record_type": None, "route": None, "target_record_id": None,
                "record_markdown": None, "claims": []}
        if "bounded exports" in text:
            target = item["matches"][0]["record_id"]
            d = dict(base, acao="UPDATE", record_type="TECHNOLOGY",
                     route="02_WEB_DEVELOPMENT/UI_COMPONENTS", target_record_id=target,
                     record_markdown=(drive.base / CREATED).read_text(),
                     claims=[{"text": "Supports bounded exports",
                              "evidence_quote": "bounded exports for sprite animation"}])
        elif item["matches"]:
            d = dict(base, acao="UNCHANGED", motivo="NO_NEW_INFORMATION")
        elif "Pixel Rig" in text:
            d = dict(base, acao="CREATE", record_type="TECHNOLOGY",
                     route="02_WEB_DEVELOPMENT/UI_COMPONENTS",
                     record_markdown=("RECORD_ID: " + expected_record_id(item["item_id"]) +
                                      "\nTYPE: TECHNOLOGY\nSTATUS: TEST\nDOMAIN: 02_WEB_DEVELOPMENT\n"
                                      "CATEGORY: UI_COMPONENTS\nTITLE: Pixel Rig Tool\n# Pixel Rig Tool\n\n"
                                      "## SUMMARY\nPixel Rig Tool enables rigging workflows for 2D sprites.\n"),
                     claims=[{"text": "Rigging for sprites",
                              "evidence_quote": "rigging workflows for 2D sprites"}])
        else:
            d = dict(base, acao="REJECT", motivo="IRRELEVANT")
        return {"decisoes": [d]}
    return ask


class CloudCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.drive = DirRemote(self.tmp / "drive")
        baseline = ("RECORD_ID: 0123456789abcdef0123\nTYPE: TECHNOLOGY\nSTATUS: REFERENCE\n"
                    "DOMAIN: 02_WEB_DEVELOPMENT\nCATEGORY: UI_COMPONENTS\nTITLE: Prior Tool\n"
                    "# Prior Tool\n\n## SUMMARY\nPrior Tool retains existing knowledge.\n").encode()
        files = {PRIOR: baseline, **planned_indexes([validate_record(PRIOR, baseline)])}
        for rel, raw in files.items():
            self.drive.write(rel, raw)
        self.drive.writes = 0
        self.saved_ops = coord._drive_ops
        coord._drive_ops = self.drive
        self.runs = 0
        from unittest.mock import patch as _p
        _g = _p("e2_gemini_decide.MIN_CALL_GAP_SECONDS", 0)
        _g.start(); self.addCleanup(_g.stop)

    def tearDown(self) -> None:
        coord._drive_ops = self.saved_ops

    def drop(self, name: str, text: str) -> None:
        p = self.drive.base / cycle.DROP / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def run_cycle(self, day: str) -> dict:
        self.runs += 1
        mirror = self.tmp / f"mirror-{self.runs}"  # fresh runner each cycle
        with patch("e2_gemini_decide.ask_gemini", side_effect=fake_gemini(self.drive)):
            return cycle.run_cycle(mirror, self.drive, COORD, day=day, api_key="test")

    def test_full_chain_create_reject_update_unchanged_and_idempotent(self) -> None:
        self.drop("a-new.txt", "Pixel Rig Tool enables rigging workflows for 2D sprites.\n")
        self.drop("b-junk.txt", "Grocery list: buy milk, eggs and bread tomorrow morning please.\n")
        r1 = self.run_cycle("2026-11-01")
        self.assertEqual(r1["e3"], {"COMMITTED_RELEASED": 1})
        self.assertEqual((r1.get("action_create"), r1.get("action_reject")), (1, 1))
        created = (self.drive.base / CREATED).read_bytes()
        self.assertIn(b"Pixel Rig Tool", created)
        self.assertIn(b"technology-pixel-rig-tool.md",
                      (self.drive.base / "00_LIBRARY/MASTER_INDEX.md").read_bytes())

        self.drop("c-update.txt", "Pixel Rig Tool supports bounded exports for sprite animation.\n")
        self.drop("d-dup.txt", "Pixel Rig Tool enables rigging workflows for 2D sprites. Still true.\n")
        r2 = self.run_cycle("2026-11-02")
        self.assertEqual(r2["e1_extracted"], 2)
        self.assertEqual(r2["e3"], {"COMMITTED_RELEASED": 1})
        self.assertEqual((r2.get("action_update"), r2.get("action_unchanged")), (1, 1))
        after = (self.drive.base / CREATED).read_bytes()
        self.assertTrue(after.startswith(created.rstrip(b"\n")))
        self.assertIn(b"bounded exports", after)
        self.assertEqual(after.splitlines()[0], created.splitlines()[0])

        r3 = self.run_cycle("2026-11-03")
        self.assertEqual((r3["e1_extracted"], r3["e2_decisions"], r3["e3"]), (0, 0, {}))
        self.assertEqual(r3["drop_new"], 0)
        _, state, _ = coord.read_state(COORD)
        self.assertEqual(state["state"], "FREE")
        self.assertEqual(len(list((self.drive.base / cycle.DROP).iterdir())), 4)  # never moved
        # Local gates never reach Drive.
        self.assertFalse((self.drive.base / cycle.e1_source_prepare.MARKER).exists())
        self.assertFalse((self.drive.base / cycle.e3_recovery_fixture.MARKER).exists())

    def test_empty_day_is_a_clean_noop(self) -> None:
        r = self.run_cycle("2026-11-01")
        self.assertEqual((r["e1_extracted"], r["e2_decisions"], r["e3"], r["recovery"]),
                         (0, 0, {}, "FREE"))

    def test_uncertain_remote_write_stops_without_release(self) -> None:
        self.drop("a-new.txt", "Pixel Rig Tool enables rigging workflows for 2D sprites.\n")
        self.drive.fail_writes_after = 4
        with self.assertRaises(cycle.UncertainRemote):
            self.run_cycle("2026-11-01")
        self.assertFalse((self.drive.base / CREATED).exists())

    def test_prewrite_active_lease_is_released_then_cycle_continues(self) -> None:
        owner = dict(transaction_id="a" * 20, batch_id="lote-x", owner="candidate-publisher-" + "0" * 12,
                     journal_path="j.json")
        self.assertEqual(coord.acquire(COORD, **owner)[2], [])
        r = self.run_cycle("2026-11-01")
        self.assertEqual(r["recovery"], "PREWRITE_RELEASED")
        self.assertEqual(coord.read_state(COORD)[1]["state"], "FREE")

    def test_started_uncommitted_lease_stops_closed(self) -> None:
        owner = dict(transaction_id="a" * 20, batch_id="lote-x", owner="candidate-publisher-" + "0" * 12,
                     journal_path="j.json")
        coord.acquire(COORD, **owner)
        coord.mark_canonical_write_started(COORD, **owner)
        with self.assertRaisesRegex(cycle.CycleError, "recovery_lot_missing|recovery_required"):
            self.run_cycle("2026-11-01")

    def test_non_lease_coordinator_rejected(self) -> None:
        with self.assertRaises(cycle.CycleError):
            cycle.run_cycle(self.tmp / "m", self.drive, "/tmp/bare.git")


    def test_one_broken_source_does_not_block_the_others(self) -> None:
        self.drop("a-new.txt", "Pixel Rig Tool enables rigging workflows for 2D sprites.\n")
        self.drop("b-broken.txt", "this one crashes the decoder\n")
        real = cycle.e1_source_prepare.extract_file

        def flaky(source, *a, **k):
            if b"crashes" in Path(source).read_bytes():
                raise RuntimeError("decoder crashed")
            return real(source, *a, **k)

        with patch.object(cycle.e1_source_prepare, "extract_file", side_effect=flaky):
            r = self.run_cycle("2026-11-01")
        self.assertEqual((r["e1_extracted"], r["e1_failed"], r["e1_lotes_written"]), (1, 1, 1))
        self.assertEqual(r["e3"], {"COMMITTED_RELEASED": 1})
        r2 = self.run_cycle("2026-11-02")  # broken file retried, healthy one not
        self.assertEqual((r2["drop_new"], r2["e1_extracted"]), (1, 1))


    def test_newer_extractor_rereads_old_sources_once(self) -> None:
        import e1_source_extract
        self.drop("a-new.txt", "Pixel Rig Tool enables rigging workflows for 2D sprites.\n")
        with patch.object(e1_source_extract, "EXTRACTOR_VERSION", 1), \
             patch.object(cycle.e1_source_prepare, "EXTRACTOR_VERSION", 1):
            r1 = self.run_cycle("2026-11-01")
        self.assertEqual(r1["e1_extracted"], 1)
        r2 = self.run_cycle("2026-11-02")  # v2: re-read once, new package
        self.assertEqual((r2["drop_new"], r2["e1_extracted"]), (1, 1))
        index = json.loads((self.drive.base / cycle.FILE_ROOT / "index.json").read_bytes())
        self.assertEqual(len(index["items"]), 1)  # old package superseded, not duplicated
        r3 = self.run_cycle("2026-11-03")
        self.assertEqual((r3["drop_new"], r3["e1_extracted"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
