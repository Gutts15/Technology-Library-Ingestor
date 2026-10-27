#!/usr/bin/env python3
"""E3 private immutable-file transport with explicit uncertainty semantics.

This is a library module with no CLI or Actions integration. It NEVER writes
to 00_LIBRARY. A future production coordinator must own canonical writes.
For every uncertain rclone result, the caller must stop and reconcile before
any new private write or releasing coordinator ownership.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

LOT_RECEIPT = re.compile(
    r"^99_INBOX/CURATION/LOTES/\d{4}-\d{2}-\d{2}/recibo(?:-\d{2})?\.json$")
RECOVERY = re.compile(
    r"^99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/[0-9a-f]{20}/"
    r"candidate-publisher-[0-9a-z]{12}/(?:journal\.json|snapshots/[0-9a-f]{20}\.bin)$")
MAX_BYTES = 256 * 1024
MAX_TIMEOUT = 75


class TransportError(Exception):
    pass


@dataclass(frozen=True)
class WriteResult:
    state: str
    sha256: str

    def __post_init__(self):
        if self.state not in {"CONFIRMED", "UNCERTAIN", "CONFLICT", "MISSING"}:
            raise ValueError("invalid_transport_state")


Runner = Callable[[list[str], int], tuple[int, bytes] | None]


def command(args: list[str], timeout: int) -> tuple[int, bytes] | None:
    try:
        result = subprocess.run(
            ["rclone", *args, "--log-level", "ERROR",
             "--retries", "1", "--low-level-retries", "1"],
            capture_output=True, check=False, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    # Do not log stderr/stdout; these may contain private storage details.
    return result.returncode, result.stdout


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def allowed_path(relative: str) -> bool:
    if not isinstance(relative, str) or "\\" in relative or "//" in relative or ".." in relative:
        return False
    return LOT_RECEIPT.fullmatch(relative) is not None or RECOVERY.fullmatch(relative) is not None


def write_new(remote: str, relative: str, raw: bytes,
              *, run: Runner = command) -> WriteResult:
    """Single immutable attempt. Failure/timeout is UNCERTAIN, never retry here."""
    if not isinstance(remote, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+:", remote):
        raise TransportError("remote_invalid")
    if not allowed_path(relative):
        raise TransportError("private_target_invalid")
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        raise TransportError("private_payload_invalid")
    sha = digest(raw)
    # The future caller must have durable intent+coordinator ownership before
    # invoking this method. Never infer success from a failed copy request.
    with tempfile.TemporaryDirectory(prefix="tl-e3-private-") as temp:
        file = Path(temp) / "payload"
        file.write_bytes(raw)
        ret = run(["copyto", str(file), remote + relative, "--immutable", "--stats", "0"],
                  MAX_TIMEOUT)
    if ret is None or ret[0] != 0:
        return WriteResult("UNCERTAIN", sha)
    readback = run(["cat", remote + relative], MAX_TIMEOUT)
    if readback is None or readback[0] != 0:
        return WriteResult("UNCERTAIN", sha)
    if digest(readback[1]) != sha:
        return WriteResult("CONFLICT", sha)
    return WriteResult("CONFIRMED", sha)


def reconcile(remote: str, relative: str, sha: str,
              *, run: Runner = command) -> WriteResult:
    """Explicit next-session check; no mutation and no implicit retry."""
    if not isinstance(remote, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+:", remote):
        raise TransportError("remote_invalid")
    if not allowed_path(relative) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise TransportError("reconcile_identity_invalid")
    response = run(["cat", remote + relative], MAX_TIMEOUT)
    if response is None or response[0] != 0:
        # No distinction between a missing file and unreachable storage without
        # a successful authenticated inventory read. Never infer absence here.
        return WriteResult("UNCERTAIN", sha)
    return WriteResult("CONFIRMED" if digest(response[1]) == sha else "CONFLICT", sha)
