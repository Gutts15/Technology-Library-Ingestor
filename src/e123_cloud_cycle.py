#!/usr/bin/env python3
"""Marco 3: one cloud E1 -> E2 -> E3 cycle over private Drive.

Reuses the existing, tested engines unchanged in behaviour:
  E1 prepare_sources -> E2 decide (Gemini free tier) -> E3 exercise_commit.

How it runs (GitHub Actions, no PC):
  1. Pull a bounded private mirror (00_LIBRARY, LOTES, receipts, evidence,
     recovery journals) into a temp dir OUTSIDE the git checkout.
  2. Recovery first: if the Drive lease is ACTIVE, settle or stop.
  3. Download up to 20 new DROP_HERE files (read-only, never moved/deleted).
  4. Run E1/E2/E3 locally; every durable write the engines make is shipped to
     Drive synchronously and read back byte for byte. Any uncertain remote
     write stops the cycle (exit 75) WITHOUT retry, rollback or lease release.

Single writer: workflow concurrency group + private Drive lease (Marco 2).
Logs carry counts only: no file names, IDs, paths or content.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import e1_lote_prepare
import e1_source_prepare
import e3_commit_fixture
import e3_recovery_fixture
from candidate_production_coordinator import read_state, release_prewrite_abort
from e1_lote_prepare import FILE_ROOT, LOTES
from e1_source_extract import MAX_INPUT_BYTES
from e3_lote_state import related_paths

DROP = "99_INBOX/DROP_HERE"
LEASE_DIR = "99_INBOX/CURATION/COORDINATOR"
MIRROR_FILTER = [
    "+ /00_LIBRARY/**",
    "+ /" + LOTES + "/**",
    "+ /" + e1_source_prepare.RECEIPTS + "/**",
    "+ /" + FILE_ROOT + "/**",
    "+ /99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/**",
    "+ /99_INBOX/READY_FOR_ANALYSIS/*/evidence-summary.json",
    "- **",
]
# Local gates only; never shipped to Drive.
LOCAL_ONLY = {e1_source_prepare.MARKER, e3_recovery_fixture.MARKER}
MAX_LISTING = 2000
MAX_FILE = 16 * 1024 * 1024
EXIT_UNCERTAIN = 75


class CycleError(Exception):
    pass


class UncertainRemote(SystemExit):
    """A remote write may have landed: stop, never retry or release."""

    def __init__(self) -> None:
        super().__init__(EXIT_UNCERTAIN)


class RcloneRemote:
    """Bounded rclone transport. stderr is never surfaced (public logs)."""

    def __init__(self, base: str) -> None:
        # base is "tl:" (production) or "tl:<synthetic prefix>/" (proof).
        if not base.startswith("tl:") or (base != "tl:" and not base.endswith("/")):
            raise CycleError("remote_base_invalid")
        self.base = base

    def _run(self, args: list[str], data: bytes | None = None, timeout: int = 120):
        command = ["rclone", *args, "--log-level", "ERROR", "--retries", "1",
                   "--low-level-retries", "2", "--contimeout", "15s", "--timeout", "60s"]
        try:
            return subprocess.run(command, input=data, capture_output=True,
                                  timeout=timeout, check=False)
        except (subprocess.TimeoutExpired, OSError):
            return subprocess.CompletedProcess(command, 124, b"", b"")

    def pull(self, root: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="tl-filter-") as temp:
            rules = Path(temp) / "filter.txt"
            rules.write_text("\n".join(MIRROR_FILTER) + "\n")
            got = self._run(["copy", self.base, str(root), "--filter-from", str(rules),
                             "--drive-skip-gdocs", "--stats", "0"], timeout=900)
        if got.returncode != 0:
            raise CycleError("mirror_pull_failed")

    def list_sources(self) -> list[dict[str, Any]]:
        got = self._run(["lsjson", self.base + DROP, "--recursive", "--files-only",
                         "--hash", "--hash-type", "sha256", "--drive-skip-gdocs"])
        if got.returncode == 3:
            return []
        if got.returncode != 0:
            raise CycleError("drop_listing_failed")
        try:
            rows = json.loads(got.stdout or b"[]")
        except ValueError:
            raise CycleError("drop_listing_invalid") from None
        if not isinstance(rows, list) or len(rows) > MAX_LISTING:
            raise CycleError("drop_listing_invalid")
        out = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("Path"), str):
                raise CycleError("drop_listing_invalid")
            sha = (row.get("Hashes") or {}).get("sha256")
            out.append({"path": row["Path"], "size": row.get("Size", -1),
                        "sha256": sha if isinstance(sha, str) else None,
                        "mtime": str(row.get("ModTime", ""))})
        return out

    def download(self, relative: str, destination: Path) -> None:
        got = self._run(["copyto", self.base + DROP + "/" + relative, str(destination),
                         "--stats", "0"], timeout=600)
        if got.returncode != 0:
            raise CycleError("source_download_failed")

    def read(self, relative: str) -> bytes | bool | None:
        """bytes when present, False when absent, None when unreachable."""
        path = self.base + relative
        listed = self._run(["lsjson", path])
        if listed.returncode == 3 or (listed.returncode == 0 and listed.stdout.strip() in (b"", b"[]")):
            return False
        if listed.returncode != 0:
            return None
        got = self._run(["cat", path], timeout=300)
        return got.stdout if got.returncode == 0 else None

    def write(self, relative: str, raw: bytes) -> bool:
        return self._run(["rcat", self.base + relative], raw, timeout=300).returncode == 0


class Shipper:
    """Replicates each local durable write to Drive with byte readback."""

    def __init__(self, root: Path, remote: Any) -> None:
        self.root, self.remote = root.resolve(), remote
        self.writes = 0
        self.local_sync = e3_recovery_fixture.sync_write

    def relative(self, path: Path) -> str:
        resolved = Path(os.path.abspath(path))
        if self.root not in resolved.parents:
            raise CycleError("write_outside_mirror")
        rel = resolved.relative_to(self.root).as_posix()
        if ".." in rel.split("/") or "//" in rel or "\\" in rel:
            raise CycleError("unsafe_relative")
        return rel

    def ship(self, path: Path, raw: bytes, *, immutable: bool) -> None:
        rel = self.relative(path)
        if rel in LOCAL_ONLY or rel.endswith(".lock"):
            return
        if immutable:
            current = self.remote.read(rel)
            if current is None:
                raise CycleError("remote_unreachable_before_write")
            if current is not False:
                if current == raw:
                    return  # already durable (resumed cycle)
                raise CycleError("remote_immutable_conflict")
        if not self.remote.write(rel, raw):
            raise UncertainRemote()
        self.writes += 1
        if self.remote.read(rel) != raw:
            raise UncertainRemote()

    def sync_write(self, path: Path, raw: bytes, *, new_only: bool = False) -> None:
        self.local_sync(path, raw, new_only=new_only)
        self.ship(path, raw, immutable=new_only)

    def install(self):
        from contextlib import ExitStack
        from unittest.mock import patch
        stack = ExitStack()
        for module in (e3_recovery_fixture, e1_source_prepare, e3_commit_fixture):
            stack.enter_context(patch.object(module, "sync_write", self.sync_write))
        original = e1_lote_prepare.Storage.create
        shipper = self

        def create(store, path, data):
            original(store, path, data)
            if store.root is not None:
                shipper.ship(Path(store.root) / path, data, immutable=True)

        stack.enter_context(patch.object(e1_lote_prepare.Storage, "create", create))
        return stack


def _write_markers(root: Path) -> None:
    for relative, raw in ((e1_source_prepare.MARKER, e1_source_prepare.MARKER_BYTES),
                          (e3_recovery_fixture.MARKER, e3_recovery_fixture.MARKER_BYTES)):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


def _known_sources(root: Path) -> set[str]:
    base = root / e1_source_prepare.RECEIPTS
    return {p.stem for p in base.glob("*.json")} if base.is_dir() else set()


def _lots(root: Path) -> list[str]:
    store = e1_lote_prepare.Storage(root)
    return store.past_lotes() if (root / LOTES).is_dir() else []


def _lot_for_batch(root: Path, batch_id: str) -> str | None:
    for relative in _lots(root):
        try:
            if json.loads((root / LOTES / relative).read_bytes()).get("lote_id") == batch_id:
                return relative
        except ValueError:
            continue
    return None


def recover_first(root: Path, coordinator: str) -> str:
    _, state, errors = read_state(coordinator)
    if errors == ["coordinator_ref_missing"]:
        return "FREE"  # first ever cycle; acquire() seeds the lease
    if errors or not isinstance(state, dict):
        raise CycleError("coordinator_unavailable")
    if state.get("state") != "ACTIVE":
        return "FREE"
    if state.get("canonical_write_performed") is False:
        # Remote truth says no canonical byte was written: release the
        # pre-write lease exactly once; a failed release stops the cycle.
        identity = {k: state.get(k) for k in ("transaction_id", "batch_id", "owner", "journal_path")}
        if release_prewrite_abort(coordinator, **identity):
            raise UncertainRemote()
        return "PREWRITE_RELEASED"
    relative = _lot_for_batch(root, str(state.get("batch_id")))
    if relative is None:
        raise CycleError("recovery_lot_missing")
    decisions, _ = related_paths(relative)
    raw_lot = (root / LOTES / relative).read_bytes()
    raw_decisions = (root / LOTES / decisions).read_bytes()
    try:
        return e3_commit_fixture.resume_terminal_commit(
            root, coordinator, raw_lot, relative, raw_decisions)
    except e3_commit_fixture.CommitFixtureError:
        # Started but not committed: needs the recovery engine; stop closed.
        raise CycleError("recovery_required") from None


def select_sources(remote: Any, known: set[str], limit: int) -> tuple[list[dict], dict[str, int]]:
    rows = remote.list_sources()
    stats = {"drop_files": len(rows), "drop_new": 0, "drop_oversize": 0, "drop_no_hash": 0}
    fresh = []
    for row in rows:
        if not isinstance(row["size"], int) or row["size"] < 0 or row["size"] > MAX_INPUT_BYTES:
            stats["drop_oversize"] += 1
            continue
        if row["sha256"] is None:
            stats["drop_no_hash"] += 1
            continue
        if row["sha256"] in known:
            continue
        fresh.append(row)
    stats["drop_new"] = len(fresh)
    unique, seen = [], set()
    for row in sorted(fresh, key=lambda r: (r["mtime"], r["sha256"])):
        if row["sha256"] not in seen:
            seen.add(row["sha256"])
            unique.append(row)
    return unique[:limit], stats


def extract_each(root: Path, paths: list[Path], day: str) -> dict[str, int]:
    """One source per call, so a heavy or broken file cannot block the others
    forever: it simply has no receipt and is retried next cycle. The lot is
    built once at the end from everything extracted."""
    from unittest.mock import patch
    totals = {"extracted": 0, "duplicates": 0, "held": 0, "lotes_written": 0, "failed": 0}
    with patch.object(e1_source_prepare, "prepare", lambda *a, **k: (None, None, 0)):
        for path in paths:
            try:
                got = e1_source_prepare.prepare_sources(root, [path], day, budget_seconds=600)
            except (UncertainRemote, CycleError):
                raise  # remote trouble is never "one bad file"
            except Exception:  # extraction/decoder failure of this one source
                totals["failed"] += 1
                continue
            for key in ("extracted", "duplicates", "held"):
                totals[key] += got[key]
    totals["lotes_written"] = e1_source_prepare.prepare_sources(root, [], day)["lotes_written"]
    return totals


def run_cycle(root: Path, remote: Any, coordinator: str, *, day: str | None = None,
              api_key: str | None = None, responses: Any = None,
              pull: bool = True) -> dict[str, Any]:
    """One E1->E2->E3 cycle. `root` must be an empty dir outside any git checkout."""
    from e2_gemini_decide import DecisionStore, decide

    if not coordinator.startswith("drive-lease:tl:"):
        raise CycleError("coordinator_must_be_drive_lease")
    day = day or datetime.now(timezone.utc).date().isoformat()
    root = Path(os.path.abspath(root))
    root.mkdir(parents=True, exist_ok=True)
    if pull:
        remote.pull(root)
    _write_markers(root)
    report: dict[str, Any] = {}
    shipper = Shipper(root, remote)
    with shipper.install():
        report["recovery"] = recover_first(root, coordinator)

        # E1: bounded new sources, downloaded read-only outside the mirror.
        chosen, stats = select_sources(remote, _known_sources(root), e1_source_prepare.MAX_SOURCES)
        report.update(stats)
        with tempfile.TemporaryDirectory(prefix="tl-sources-") as temp:
            paths = []
            for index, row in enumerate(chosen):
                suffix = Path(row["path"]).suffix.lower()[:12]
                local = Path(temp) / f"{index:02d}{suffix}"
                remote.download(row["path"], local)
                if hashlib.sha256(local.read_bytes()).hexdigest() != row["sha256"]:
                    raise CycleError("source_changed_during_download")
                paths.append(local)
            e1 = extract_each(root, paths, day)
        report.update({"e1_" + k: v for k, v in e1.items()})

        # E2: oldest ready lot without decisions (at most one per cycle).
        count, written = decide(DecisionStore(root), responses=responses,
                                api_key=api_key, apply=True)
        report["e2_decisions"], report["e2_written"] = count, written
        import e2_gemini_decide
        report["e2_hold_codes"] = dict(e2_gemini_decide.HOLD_CODES)

        # E3: settle every lot that has decisions but no terminal receipt.
        outcomes: dict[str, int] = {}
        for relative in _lots(root):
            decisions, receipt = related_paths(relative)
            if not (root / LOTES / decisions).is_file() or (root / LOTES / receipt).is_file():
                continue
            result, _ = e3_commit_fixture.exercise_commit(
                root, coordinator, (root / LOTES / relative).read_bytes(), relative,
                (root / LOTES / decisions).read_bytes())
            outcomes[result] = outcomes.get(result, 0) + 1
            if result == "RECOVERY_REQUIRED_OWNED":
                raise CycleError("recovery_required")
            for row in json.loads((root / LOTES / decisions).read_bytes())["decisoes"]:
                key = "action_" + str(row.get("acao", "?")).lower()
                report[key] = report.get(key, 0) + 1
        report["e3"] = outcomes
    report["remote_writes"] = shipper.writes
    return report


def main() -> int:
    base = os.environ.get("TL_CYCLE_BASE", "tl:")
    coordinator = "drive-lease:" + base + LEASE_DIR
    work = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "tl-cycle-mirror"
    try:
        if work.exists():
            raise CycleError("mirror_dir_not_empty")
        report = run_cycle(work, RcloneRemote(base), coordinator,
                           api_key=os.environ.get("GEMINI_API_KEY") or None)
    except UncertainRemote:
        print("e123_cycle status=UNCERTAIN_STOPPED canonical_retry=0")
        return EXIT_UNCERTAIN
    except CycleError as exc:
        print("e123_cycle status=BLOCKED code=" + str(exc))
        return 2
    except Exception as exc:  # never print private content
        # Engine exceptions carry fixed codes; anything else only its class.
        coded = type(exc).__name__ in {"SourcePreparationError", "DecisionError",
                                       "CommitFixtureError", "PreparationError",
                                       "FixtureRecoveryError", "CoordinatorFixtureError"}
        print("e123_cycle status=BLOCKED code=" + (str(exc) if coded else type(exc).__name__))
        return 2
    print("e123_cycle status=OK " + " ".join(
        f"{k}={json.dumps(v, sort_keys=True, separators=(',', ':'))}" for k, v in sorted(report.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
