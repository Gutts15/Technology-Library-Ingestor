#!/usr/bin/env python3
"""E1 safe lot preparation from hash-bound FILE_EVIDENCE envelopes.

Read-only by default. Only private LOTES may be written via --apply;
no scheduling, model invocation, canonical publication or source deletion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from evidence_quality import assess_evidence, usable_summary
from file_evidence_candidate_bridge import bound_envelope, FILE_ROOT
from ready_evidence_bridge import read_json_bytes

LOTES = "99_INBOX/CURATION/LOTES"
MASTER = "00_LIBRARY/MASTER_INDEX.md"
ID_RE = re.compile(r"^[0-9a-f]{20}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
LOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}/lote(?:-\d{2})?\.json$")
INDEX_LINE = re.compile(r"^- (TECHNOLOGY|PATTERN|PIPELINE) - (.+?) \[(?:REFERENCE|TEST|DEPRECATED)\] \(\x60(00_LIBRARY/[^\x60]+\.md)\x60\)$")
MAX_ITEMS = 20


class PreparationError(Exception):
    pass


def encode(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


class Storage:
    def __init__(self, root: Path | None, remote: str = "tl:"):
        self.root, self.remote = root, remote

    def command(self, *args: str) -> bytes:
        try:
            proc = subprocess.run(["rclone", *args, "--log-level", "ERROR",
                                   "--retries", "1", "--low-level-retries", "1"],
                                  capture_output=True, timeout=90, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise PreparationError("storage_unavailable") from None
        if proc.returncode:
            raise PreparationError("storage_unavailable")
        return proc.stdout

    def read(self, path: str, max_bytes: int = 200 * 1024) -> bytes:
        if self.root is None:
            raw = self.command("cat", self.remote + path)
        else:
            try:
                raw = (self.root / path).read_bytes()
            except OSError:
                raise PreparationError("private_input_missing") from None
        if len(raw) > max_bytes:
            raise PreparationError("private_input_oversize")
        return raw

    def past_lotes(self) -> list[str]:
        if self.root is None:
            try:
                listing = json.loads(self.command("lsjson", self.remote + LOTES,
                                                  "--recursive", "--files-only"))
            except ValueError:
                raise PreparationError("lote_inventory_invalid") from None
            if not isinstance(listing, list) or len(listing) > 1000:
                raise PreparationError("lote_inventory_invalid")
            paths = [item.get("Path") for item in listing if isinstance(item, dict)]
        else:
            base = self.root / LOTES
            paths = [p.relative_to(base).as_posix() for p in base.glob("*/lote*.json")]
        if len(paths) > 1000 or not all(isinstance(p, str) for p in paths):
            raise PreparationError("lote_inventory_invalid")
        return sorted(path for path in paths if LOT_RE.fullmatch(path))

    def create(self, path: str, data: bytes) -> None:
        if self.root is not None:
            file = self.root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            try:
                with file.open("xb") as handle:
                    handle.write(data)
            except FileExistsError:
                if file.read_bytes() != data:
                    raise PreparationError("lote_conflict") from None
            if file.read_bytes() != data:
                raise PreparationError("lote_readback_failed")
            return
        with tempfile.TemporaryDirectory(prefix="tl-e1-") as temp:
            file = Path(temp) / "lote.json"
            file.write_bytes(data)
            # Never overwrite; uncertain failures require external reconciliation.
            self.command("copyto", str(file), self.remote + path,
                         "--immutable", "--stats", "0")
        if self.read(path) != data:
            raise PreparationError("lote_readback_failed")


def validate_index(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("type") != "FILE_EVIDENCE_INDEX":
        raise PreparationError("index_invalid")
    rows = value.get("items")
    if not isinstance(rows, list) or len(rows) > 200 or value.get("active") != len(rows):
        raise PreparationError("index_invalid")
    seen = set()
    output = []
    for row in rows:
        if not isinstance(row, dict):
            raise PreparationError("index_invalid")
        fields = {key: row.get(key) for key in ("package_id", "revision_key", "envelope_id",
                                               "evidence_summary_sha256", "path")}
        pid = fields["package_id"]
        if (not all(isinstance(v, str) for v in fields.values())
                or any(not ID_RE.fullmatch(fields[k]) for k in ("package_id", "revision_key", "envelope_id"))
                or not SHA_RE.fullmatch(fields["evidence_summary_sha256"])
                or fields["path"] != FILE_ROOT + "/" + pid + ".json"
                or pid in seen):
            raise PreparationError("index_invalid")
        seen.add(pid)
        output.append(fields)
    return sorted(output, key=lambda item: item["package_id"])


def past_state(store: Storage) -> tuple[set[str], bool, set[str]]:
    seen = set()
    pending = False
    names = set()
    for subpath in store.past_lotes():
        data = read_json_bytes(store.read(LOTES + "/" + subpath))
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise PreparationError("previous_lote_invalid")
        if data.get("status") not in {"PRONTO", "APLICADO", "REJEITADO", "HOLD"}:
            raise PreparationError("previous_lote_invalid")
        rows = data.get("itens")
        if not isinstance(rows, list) or len(rows) > MAX_ITEMS:
            raise PreparationError("previous_lote_invalid")
        for row in rows:
            key = row.get("item_id") if isinstance(row, dict) else None
            if not isinstance(key, str) or not ID_RE.fullmatch(key) or key in seen:
                raise PreparationError("previous_lote_invalid")
            seen.add(key)
        pending |= data["status"] == "PRONTO"
        names.add(subpath)
    return seen, pending, names


def index_matches(store: Storage, master: str, evidence: dict[str, Any]) -> list[dict]:
    text = json.dumps(evidence, ensure_ascii=False).casefold()
    matches = []
    for line in master.splitlines():
        record = INDEX_LINE.fullmatch(line)
        if not record:
            continue
        record_type, title, path = record.groups()
        if len(title) < 5 or title.casefold() not in text:
            continue
        raw = store.read(path, max_bytes=128 * 1024).decode("utf-8")
        record_id = re.search(r"^RECORD_ID: ([0-9a-f]{20})$", raw, re.MULTILINE)
        if record_id is None:
            raise PreparationError("match_record_invalid")
        summary = re.search(r"(?ms)^## SUMMARY\s*\n(.*?)(?:\n## |\Z)", raw)
        matches.append({"record_id": record_id.group(1), "type": record_type,
                        "title": title, "summary": (summary.group(1).strip() if summary else "")[:500]})
        if len(matches) == 3:
            break
    return matches


def prepare(store: Storage, day: str) -> tuple[dict | None, str | None, int]:
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("invalid_date")
    prior, pending, names = past_state(store)
    if pending:
        return None, None, 0
    index = validate_index(read_json_bytes(store.read(FILE_ROOT + "/index.json", 128 * 1024)))
    master = store.read(MASTER, 128 * 1024).decode("utf-8")
    selected: list[dict] = []
    held = 0
    for indexed in index:
        envelope = read_json_bytes(store.read(indexed["path"], 24 * 1024))
        evidence_path = "99_INBOX/READY_FOR_ANALYSIS/" + indexed["package_id"] + "/evidence-summary.json"
        binding, problem = bound_envelope(indexed, envelope, store.read(evidence_path, 16 * 1024))
        if problem is not None or binding is None or not isinstance(envelope, dict):
            raise PreparationError("evidence_binding_invalid")
        assessment = assess_evidence(binding["kind"], binding["semantic_summary"])
        if envelope.get("evidence_assessment") != assessment:
            raise PreparationError("evidence_assessment_invalid")
        if assessment["state"] != "ELIGIBLE":
            held += 1
            continue
        semantic = usable_summary(binding["kind"], binding["semantic_summary"])
        compact = {key: semantic[key] for key in ("evidence", "link", "content", "extraction")
                   if key in semantic}
        if len(encode(compact)) > 3000:
            held += 1
            continue
        key = hashlib.sha256(("e1|" + indexed["package_id"] + "|" +
                              indexed["revision_key"] + "|" +
                              indexed["evidence_summary_sha256"]).encode()).hexdigest()[:20]
        if key in prior:
            continue
        selected.append({"item_id": key, "package_id": indexed["package_id"],
                         "revision_key": indexed["revision_key"], "kind": binding["kind"],
                         "evidence_sha256": indexed["evidence_summary_sha256"],
                         "evidence_assessment": assessment, "evidence_compact": compact,
                         "matches": index_matches(store, master, compact)})
        if len(selected) == MAX_ITEMS:
            break
    if not selected:
        return None, None, held
    number = 1
    while (day + "/" + ("lote.json" if number == 1 else f"lote-{number:02d}.json")) in names:
        number += 1
        if number > 99:
            raise PreparationError("lote_limit")
    filename = "lote.json" if number == 1 else f"lote-{number:02d}.json"
    result = {"schema_version": 1, "lote_id": f"{day}-{number:02d}",
              "created_date_brt": day, "status": "PRONTO", "itens": selected}
    result["lote_sha256"] = hashlib.sha256(encode(result)).hexdigest()
    if len(encode(result)) > 200 * 1024:
        raise PreparationError("lote_budget")
    return result, LOTES + "/" + day + "/" + filename, held


def main() -> int:
    parser = argparse.ArgumentParser(description="E1 immutable lote builder")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--root-dir", type=Path)
    source.add_argument("--remote")
    parser.add_argument("--date-brt", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    store = Storage(args.root_dir, args.remote or "tl:")
    try:
        lote, path, held = prepare(store, args.date_brt)
        if lote is not None and args.apply:
            store.create(path, encode(lote))
    except (PreparationError, ValueError, UnicodeError) as exc:
        reason = str(exc) if isinstance(exc, PreparationError) else "invalid_input"
        print("e1_error code=" + reason + " canonical_write=0")
        return 2
    print("e1_ok items=" + str(len(lote["itens"]) if lote else 0) +
          " held=" + str(held) + " lotes_written=" +
          str(int(lote is not None and args.apply)) +
          " canonical_write=0 model_calls=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
