#!/usr/bin/env python3
"""One-shot E2 Gemini real-model / private-Drive synthetic acceptance probe.

No canonical writes, no real inbox reads, no uploads of evidence. Only the
fixed-size G0.3b synthetic fixture is verified. Emits metadata-only receipt to
private _teste; no model response, prompt, token or source in public logs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from e1_lote_prepare import encode
from e2_gemini_decide import MODEL, DecisionError, ask_gemini, validate_model_output
from g03b_gemini_receipt import (
    INPUT_BYTES, INPUT_SHA256, ProbeError, REMOTE, RcloneStorage,
)

BRT = ZoneInfo("America/Sao_Paulo")
PROBE_KIND = "E2_SYNTHETIC_GEMINI"
PREFIX = "e2-synthetic-recibo-"
MIN_ALLOWED = {"HOLD", "REJECT"}
DESCRIPTION = ("A fictional mock technical claim with no independently "
               "verifiable material. The synthetic statement has no valid "
               "source evidence for publication in a technology library.")


def id_for(prefix: str) -> str:
    return hashlib.sha256(("e2-smoke-v1|" + prefix).encode("utf-8")).hexdigest()[:20]


def synthetic_lote(now: datetime) -> dict:
    date_brt = now.astimezone(BRT).date().isoformat()
    package_id = id_for("package")
    rev = id_for("revision")
    sha = hashlib.sha256(DESCRIPTION.encode("utf-8")).hexdigest()
    item_id = hashlib.sha256(("e1|" + package_id + "|" + rev + "|" + sha).encode()).hexdigest()[:20]
    item = {
        "item_id": item_id,
        "package_id": package_id,
        "revision_key": rev,
        "evidence_sha256": sha,
        "kind": "text",
        "evidence_assessment": {"state": "ELIGIBLE"},
        "evidence_compact": {"evidence": {"samples": [DESCRIPTION]}},
        "matches": [],
    }
    items = [item]
    return {"schema_version": 1, "lote_id": date_brt + "-01",
            "created_at_utc": now.astimezone(timezone.utc).isoformat(),
            "created_date_brt": date_brt, "status": "PRONTO",
            "itens": items,
            "items_sha256": hashlib.sha256(encode(items)).hexdigest()}


def receipt_name(day: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ProbeError("DAY_INVALID")
    return PREFIX + day + ".json"


def check_receipt(raw: bytes, day: str, synthetic_id: str) -> dict:
    if len(raw) > 4096:
        raise ProbeError("EXISTING_RECEIPT_OVERSIZE")
    try:
        obj = json.loads(raw)
        if (not isinstance(obj, dict) or obj.get("schema_version") != 1
                or obj.get("probe") != PROBE_KIND or obj.get("status") != "PASS"
                or obj.get("date_brt") != day or obj.get("model") != MODEL
                or obj.get("synthetic_item_id") != synthetic_id
                or obj.get("fixture_sha256") != INPUT_SHA256
                or obj.get("action") not in MIN_ALLOWED):
            raise ValueError()
    except (ValueError, TypeError):
        raise ProbeError("EXISTING_RECEIPT_INVALID") from None
    return obj


def run_probe(storage: RcloneStorage, *, model_call, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(BRT).date().isoformat()
    raw_fixture = storage.read_input()
    if len(raw_fixture) != INPUT_BYTES or hashlib.sha256(raw_fixture).hexdigest() != INPUT_SHA256:
        raise ProbeError("FIXTURE_MISMATCH")
    # Fixture bytes never sent to Gemini. This is the same isolated fixture
    # that already passed G0.3b readback, not any user DROP_HERE content.
    lote = synthetic_lote(now)
    item = lote["itens"][0]
    name = receipt_name(day)
    count = storage.entries(name)
    if count > 1:
        raise ProbeError("DUPLICATE_RECEIPT")
    if count == 1:
        check_receipt(storage.read(name), day, item["item_id"])
        return {"status": "ALREADY_EXISTS", "receipts_written": 0}
    try:
        response = model_call(item)
        decisions = validate_model_output(lote, response)
    except DecisionError:
        raise ProbeError("MODEL_CONTRACT_INVALID") from None
    if len(decisions) != 1 or decisions[0]["acao"] not in MIN_ALLOWED:
        raise ProbeError("UNSAFE_SYNTHETIC_DECISION")
    receipt = {
        "schema_version": 1, "probe": PROBE_KIND, "status": "PASS",
        "date_brt": day, "model": MODEL,
        "fixture_sha256": INPUT_SHA256,
        "synthetic_item_id": item["item_id"],
        "action": decisions[0]["acao"],
        "decision_sha256": hashlib.sha256(encode(response)).hexdigest(),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "code_sha": os.environ.get("GITHUB_SHA", ""),
    }
    content = encode(receipt)
    if len(content) > 4096:
        raise ProbeError("RECEIPT_OVERSIZE")
    if storage.entries(name):
        raise ProbeError("RECEIPT_APPEARED")
    with tempfile.TemporaryDirectory(prefix="e2-receipt-") as temp:
        file = Path(temp) / name
        file.write_bytes(content)
        try:
            storage.command(
                ["copyto", str(file), REMOTE + "/" + name,
                 "--immutable", "--stats", "0"], write=True)
        except ProbeError as exc:
            if str(exc) != "UPLOAD_UNCERTAIN":
                raise
            # Never retry a write when the response has been lost.
    if storage.entries(name) != 1 or storage.read(name) != content:
        raise ProbeError("RECEIPT_NOT_CONFIRMED")
    return {"status": "PASS", "receipts_written": 1,
            "action": decisions[0]["acao"]}


def main() -> int:
    try:
        if os.environ.get("GITHUB_REF") != "refs/heads/main":
            raise ProbeError("WRONG_REF")
        key = os.environ.get("E2_SMOKE_KEY", "")
        config = os.environ.get("TL_RCLONE_CONFIG", "")
        if not key or not config:
            raise ProbeError("SECRET_MISSING")
        os.umask(0o077)
        with tempfile.TemporaryDirectory(prefix="e2-smoke-", dir=os.environ.get("RUNNER_TEMP")) as temp:
            root = Path(temp)
            cfg = root / "rclone.conf"
            cfg.write_text(config, encoding="utf-8")
            result = run_probe(
                RcloneStorage(cfg, root),
                model_call=lambda item: ask_gemini(item, key),
            )
        print("e2_smoke " + " ".join(f"{k}={v}" for k, v in result.items()))
        return 0
    except ProbeError as exc:
        print("e2_smoke status=" + str(exc) + " receipts_written=0")
        return 1
    except Exception:
        # This must never expose private source, API body, or credential paths.
        print("e2_smoke status=UNEXPECTED_FAILURE receipts_written=0")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
