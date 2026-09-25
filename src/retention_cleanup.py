#!/usr/bin/env python3
"""Quarantine-first provider-neutral retention cleanup.

Processed private sources are never deleted immediately when retention expires.
The first stage moves an eligible source to a private quarantine folder. A
second grace window must expire before purge. ERROR sources are outside this
module's scope. Normal output contains counts only, never private filenames.

Destructive changes require --apply. Without it the command is a dry run.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from package_contracts import required_package_files

DEFAULT_RECORDS = "99_INBOX/PROCESS_LOG/RECORDS"
DEFAULT_READY = "99_INBOX/READY_FOR_ANALYSIS"
DEFAULT_QUARANTINE = "99_INBOX/RETENTION_TRASH"
DEFAULT_QUARANTINE_DAYS = 7
DEFAULT_MAX_RECORDS = 500
DEFAULT_RECOVERABLE_DAYS = 7
DEFAULT_NONRECOVERABLE_DAYS = 30
PACKAGE_ID_RE = re.compile(r"^[0-9a-f]{20}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def ensure_remote_dir(remote_root: str, relative: str) -> bool:
    return run_rclone(["mkdir", join_remote(remote_root, relative), "--log-level", "ERROR"]).returncode == 0


def remote_exists(remote_path: str) -> bool:
    return run_rclone(["lsjson", remote_path, "--stat", "--log-level", "ERROR"]).returncode == 0


def read_remote_json(remote_path: str) -> dict[str, Any] | None:
    result = run_rclone(["cat", remote_path, "--log-level", "ERROR"])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def write_remote_json(remote_path: str, payload: dict[str, Any], workspace: Path, safe_name: str) -> bool:
    workspace.mkdir(parents=True, exist_ok=True)
    local = workspace / safe_name
    local.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result = run_rclone(["copyto", str(local), remote_path, "--log-level", "ERROR", "--stats", "0"])
    local.unlink(missing_ok=True)
    return result.returncode == 0


def list_records(remote_root: str, folder: str) -> list[dict[str, Any]] | None:
    result = run_rclone(["lsjson", join_remote(remote_root, folder), "--files-only", "--log-level", "ERROR"])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    items = [item for item in payload if isinstance(item, dict)]
    items.sort(key=lambda item: str(item.get("ModTime") or ""))
    return items


def package_id(record: dict[str, Any]) -> str | None:
    value = record.get("package_id")
    return value if isinstance(value, str) and PACKAGE_ID_RE.fullmatch(value) else None


def source_name(record: dict[str, Any]) -> str | None:
    source = record.get("source")
    if not isinstance(source, dict):
        return None
    value = source.get("name")
    return PurePosixPath(value).name if isinstance(value, str) and value else None


def source_folder(record: dict[str, Any]) -> str | None:
    source = record.get("source")
    if not isinstance(source, dict):
        return None
    value = source.get("source_folder")
    return value.strip("/") if isinstance(value, str) and value.strip("/") else None


def processing(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("processing")
    return value if isinstance(value, dict) else {}


def routing(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("routing")
    return value if isinstance(value, dict) else {}


def retention_state(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("retention")
    return value if isinstance(value, dict) else {}


def package_complete(remote_root: str, ready: str, pkg: str, record: dict[str, Any]) -> bool:
    required = required_package_files(source_folder(record))
    if not required:
        return False
    base = str(PurePosixPath(ready) / pkg)
    return all(
        remote_exists(join_remote(remote_root, str(PurePosixPath(base) / name)))
        for name in required
    )


def retention_days(record: dict[str, Any]) -> int:
    route = routing(record)
    explicit = route.get("retention_days")
    try:
        if explicit is not None:
            return max(1, int(explicit))
    except (TypeError, ValueError):
        pass
    recoverable = route.get("source_recoverable")
    return DEFAULT_RECOVERABLE_DAYS if recoverable is True else DEFAULT_NONRECOVERABLE_DAYS


def candidate_source_paths(remote_root: str, folder: str, name: str, pkg: str) -> list[tuple[str, str]]:
    base = PurePosixPath(name)
    alternate = f"{base.stem or 'source'}.{pkg}{base.suffix}"
    return [
        (join_remote(remote_root, str(PurePosixPath(folder) / name)), name),
        (join_remote(remote_root, str(PurePosixPath(folder) / alternate)), alternate),
    ]


def find_existing_source(remote_root: str, folder: str, name: str, pkg: str) -> tuple[str, str] | None:
    for path, resolved_name in candidate_source_paths(remote_root, folder, name, pkg):
        if remote_exists(path):
            return path, resolved_name
    return None


def safe_quarantine_destination(remote_root: str, quarantine: str, name: str, pkg: str) -> tuple[str, str]:
    normal = join_remote(remote_root, str(PurePosixPath(quarantine) / name))
    if not remote_exists(normal):
        return normal, name
    base = PurePosixPath(name)
    alternate = f"{base.stem or 'source'}.{pkg}{base.suffix}"
    return join_remote(remote_root, str(PurePosixPath(quarantine) / alternate)), alternate


def sync_record(remote_root: str, records: str, ready: str, pkg: str, record: dict[str, Any], workspace: Path) -> bool:
    central = join_remote(remote_root, str(PurePosixPath(records) / f"{pkg}.json"))
    if not write_remote_json(central, record, workspace, f"retention-{pkg}.json"):
        return False
    package_record = join_remote(remote_root, str(PurePosixPath(ready) / pkg / "process-record.json"))
    if remote_exists(package_record):
        return write_remote_json(package_record, record, workspace, f"retention-package-{pkg}.json")
    return True


def eligible_for_quarantine(record: dict[str, Any], now: datetime) -> bool:
    proc = processing(record)
    if proc.get("status") != "processed":
        return False
    route = routing(record)
    if bool(route.get("keep_original")):
        return False
    processed_at = parse_time(proc.get("processed_at"))
    if processed_at is None:
        return False
    return now >= processed_at + timedelta(days=retention_days(record))


def quarantine_record(
    *,
    remote_root: str,
    records: str,
    ready: str,
    quarantine: str,
    record: dict[str, Any],
    pkg: str,
    name: str,
    workspace: Path,
    apply: bool,
) -> str:
    route = routing(record)
    source_folder_value = route.get("destination_folder")
    if not isinstance(source_folder_value, str) or not source_folder_value.strip("/"):
        return "guarded"
    existing = find_existing_source(remote_root, source_folder_value.strip("/"), name, pkg)
    if existing is None:
        return "missing"
    source_path, resolved_source_name = existing
    destination, quarantine_name = safe_quarantine_destination(remote_root, quarantine, resolved_source_name, pkg)
    if not apply:
        return "eligible"
    result = run_rclone(["moveto", source_path, destination, "--log-level", "ERROR", "--stats", "0"])
    if result.returncode != 0:
        return "error"
    record["retention"] = {
        "state": "quarantined",
        "quarantined_at": iso_now(),
        "original_folder": source_folder_value.strip("/"),
        "quarantine_folder": quarantine.strip("/"),
        "quarantine_name": quarantine_name,
        "retention_days": retention_days(record),
    }
    return "quarantined" if sync_record(remote_root, records, ready, pkg, record, workspace) else "error"


def purge_record(
    *,
    remote_root: str,
    records: str,
    ready: str,
    record: dict[str, Any],
    pkg: str,
    workspace: Path,
    quarantine_days: int,
    now: datetime,
    apply: bool,
) -> str:
    state = retention_state(record)
    if state.get("state") != "quarantined":
        return "not_quarantined"
    quarantined_at = parse_time(state.get("quarantined_at"))
    folder = state.get("quarantine_folder")
    name = state.get("quarantine_name")
    if quarantined_at is None or not isinstance(folder, str) or not isinstance(name, str):
        return "guarded"
    if now < quarantined_at + timedelta(days=quarantine_days):
        return "waiting"
    path = join_remote(remote_root, str(PurePosixPath(folder) / PurePosixPath(name).name))
    if not remote_exists(path):
        return "missing"
    if not apply:
        return "purge_eligible"
    result = run_rclone(["deletefile", path, "--log-level", "ERROR"])
    if result.returncode != 0:
        return "error"
    record["retention"] = {
        **state,
        "state": "purged",
        "purged_at": iso_now(),
        "quarantine_days": quarantine_days,
    }
    return "purged" if sync_record(remote_root, records, ready, pkg, record, workspace) else "error"


def cleanup(
    *,
    remote_root: str,
    workspace: Path,
    records: str,
    ready: str,
    quarantine: str,
    quarantine_days: int,
    max_records: int,
    apply: bool,
    now: datetime | None = None,
) -> tuple[bool, dict[str, int]]:
    current = now or utc_now()
    stats = {
        "scanned": 0,
        "preserved": 0,
        "eligible": 0,
        "quarantined": 0,
        "waiting": 0,
        "purge_eligible": 0,
        "purged": 0,
        "missing": 0,
        "guarded": 0,
        "errors": 0,
    }

    for folder in (records, ready, quarantine):
        if not ensure_remote_dir(remote_root, folder):
            stats["errors"] += 1
            return False, stats

    items = list_records(remote_root, records)
    if items is None:
        stats["errors"] += 1
        return False, stats

    for item in items[:max_records]:
        path = item.get("Path") or item.get("Name")
        if not isinstance(path, str) or not path.endswith(".json"):
            continue
        record = read_remote_json(join_remote(remote_root, str(PurePosixPath(records) / path)))
        if not record:
            stats["errors"] += 1
            continue
        pkg = package_id(record)
        name = source_name(record)
        if not pkg or not name:
            stats["guarded"] += 1
            continue

        stats["scanned"] += 1
        proc = processing(record)
        route = routing(record)
        state = retention_state(record)

        if proc.get("status") != "processed" or bool(route.get("keep_original")):
            stats["preserved"] += 1
            continue
        if not package_complete(remote_root, ready, pkg, record):
            stats["guarded"] += 1
            continue
        if state.get("state") == "purged":
            stats["preserved"] += 1
            continue
        if state.get("state") == "quarantined":
            outcome = purge_record(
                remote_root=remote_root,
                records=records,
                ready=ready,
                record=record,
                pkg=pkg,
                workspace=workspace,
                quarantine_days=quarantine_days,
                now=current,
                apply=apply,
            )
            if outcome in stats:
                stats[outcome] += 1
            elif outcome == "error":
                stats["errors"] += 1
            elif outcome == "not_quarantined":
                stats["guarded"] += 1
            continue

        if not eligible_for_quarantine(record, current):
            stats["preserved"] += 1
            continue
        outcome = quarantine_record(
            remote_root=remote_root,
            records=records,
            ready=ready,
            quarantine=quarantine,
            record=record,
            pkg=pkg,
            name=name,
            workspace=workspace,
            apply=apply,
        )
        if outcome in stats:
            stats[outcome] += 1
        elif outcome == "error":
            stats["errors"] += 1

    return stats["errors"] == 0, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Quarantine and purge expired processed sources safely.")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--records", default=DEFAULT_RECORDS)
    parser.add_argument("--ready", default=DEFAULT_READY)
    parser.add_argument("--quarantine", default=DEFAULT_QUARANTINE)
    parser.add_argument("--quarantine-days", type=int, default=DEFAULT_QUARANTINE_DAYS)
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS)
    parser.add_argument("--apply", action="store_true", help="Perform moves/deletes. Without this, run as dry-run.")
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("retention_error code=rclone_missing")
        return 2
    if args.quarantine_days < 1 or args.max_records < 1:
        parser.error("quarantine-days and max-records must be positive")

    ok, stats = cleanup(
        remote_root=args.remote,
        workspace=args.workspace.resolve(),
        records=args.records,
        ready=args.ready,
        quarantine=args.quarantine,
        quarantine_days=args.quarantine_days,
        max_records=args.max_records,
        apply=args.apply,
    )
    mode = "apply" if args.apply else "dry_run"
    if not ok:
        print(f"retention_error code=cleanup_failed mode={mode} errors={stats['errors']}")
        return 2

    print(
        "retention_ok "
        f"mode={mode} scanned={stats['scanned']} preserved={stats['preserved']} "
        f"eligible={stats['eligible']} quarantined={stats['quarantined']} "
        f"waiting={stats['waiting']} purge_eligible={stats['purge_eligible']} "
        f"purged={stats['purged']} missing={stats['missing']} guarded={stats['guarded']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
