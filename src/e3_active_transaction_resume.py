#!/usr/bin/env python3
"""E3 automatic discovery of the currently active durable transaction.

By default this is a strictly read-only inspection, suitable for a future
authorized scheduler. It does not create Git refs, start publication, mutate
Drive, or release a coordinator. An explicit in-process approval and
injected release callback are required to reconcile an already COMMITTED lot.

No workflow with GitHub write permissions is installed by this module.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from candidate_committed_reconciliation import (
    ROOT, _unique_pairs, classify_restart, reconcile_committed_release,
)

JournalReader = Callable[[str], bytes | None]
CoordinatorReader = Callable[[], tuple[str | None, dict[str, Any] | None, list[str]]]
Release = Callable[..., list[str]]

TX_RE = re.compile(r"^[0-9a-f]{20}$")
OWNER_RE = re.compile(r"^candidate-publisher-[0-9a-f]{12}$")
MAX_JOURNAL_BYTES = 128 * 1024
MAX_RECEIPT_BYTES = 128 * 1024


def inspect_or_reconcile(
    *, read_coordinator: CoordinatorReader,
    read_remote: JournalReader,
    allow_committed_release: bool = False,
    release_once: Release | None = None,
) -> tuple[str, list[str]]:
    """Determine or safely resume one exact ACTIVE transaction.

    The default path calls only read functions. Release is optional and can
    happen only for an already verified COMMITTED transaction with a valid
    exact Git identity. A lost Git ACK is followed by read-only reconciliation.
    No other state is mutated here.
    """
    _, owner, errors = read_coordinator()
    if errors or not isinstance(owner, dict):
        return "BLOCKED", ["coordinator_unavailable"]
    if owner.get("state") == "FREE":
        return "IDLE", []
    if owner.get("state") != "ACTIVE":
        return "BLOCKED", ["coordinator_state_invalid"]
    tx = owner.get("transaction_id")
    user_owner = owner.get("owner")
    path = owner.get("journal_path")
    if (not isinstance(tx, str) or not TX_RE.fullmatch(tx)
            or not isinstance(user_owner, str)
            or not OWNER_RE.fullmatch(user_owner)
            or path != ROOT + tx + "/" + user_owner + "/journal.json"):
        return "BLOCKED", ["coordinator_path_invalid"]

    raw = read_remote(path)
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_JOURNAL_BYTES:
        return "BLOCKED", ["journal_unavailable"]
    try:
        journal = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError):
        return "BLOCKED", ["journal_invalid"]
    if (not isinstance(journal, dict) or journal.get("journal_path") != path
            or any(journal.get(key) != owner.get(key)
                   for key in ("transaction_id", "batch_id", "owner", "journal_path"))
            or journal.get("run_root") != ROOT + tx + "/" + user_owner):
        return "BLOCKED", ["journal_owner_mismatch"]

    receipt_raw = None
    if journal.get("state") == "COMMITTED":
        receipt_raw = read_remote(journal["run_root"] + "/receipt.json")
        if (not isinstance(receipt_raw, bytes)
                or not 0 < len(receipt_raw) <= MAX_RECEIPT_BYTES):
            return "BLOCKED", ["receipt_unavailable"]

    disposition, problems = classify_restart(
        journal, owner, read_canonical=read_remote,
        read_snapshot=read_remote, receipt_raw=receipt_raw)
    if problems or disposition != "RECONCILE_COMMITTED":
        return disposition, problems

    if not allow_committed_release:
        return disposition, []
    if release_once is None:
        return "BLOCKED", ["release_callback_unavailable"]
    return reconcile_committed_release(
        journal, receipt_raw, read_canonical=read_remote,
        read_private=read_remote, read_owner=read_coordinator,
        release_once=release_once,
    )
