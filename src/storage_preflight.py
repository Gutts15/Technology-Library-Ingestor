#!/usr/bin/env python3
"""Sanitized connectivity preflight for an rclone-backed private inbox.

The command intentionally never echoes rclone stderr because provider errors may
contain account identifiers, remote paths, or credential-adjacent details. It
maps failures to a small set of operational codes that are safe for CI logs.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys


AUTH_HINTS = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "access denied",
    "access_denied",
    "invalid_grant",
    "invalid_client",
    "authentication",
    "authorization",
    "account disabled",
    "account has been disabled",
    "suspended",
    "token expired",
    "expired token",
)
RATE_LIMIT_HINTS = (
    "429",
    "rate limit",
    "too many requests",
    "quota exceeded",
)
NETWORK_HINTS = (
    "timeout",
    "timed out",
    "temporary failure",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "no such host",
    "tls handshake timeout",
)
NOT_FOUND_HINTS = (
    "404",
    "not found",
    "directory not found",
    "object not found",
    "path not found",
)


def classify_failure(stderr: str) -> str:
    text = stderr.lower()
    if any(hint in text for hint in RATE_LIMIT_HINTS):
        return "rate_limited"
    if any(hint in text for hint in AUTH_HINTS):
        return "auth_or_access_failed"
    if any(hint in text for hint in NETWORK_HINTS):
        return "network_failed"
    if any(hint in text for hint in NOT_FOUND_HINTS):
        return "remote_not_found"
    return "remote_unavailable"


def run_preflight(remote: str) -> tuple[bool, str]:
    if not shutil.which("rclone"):
        return False, "rclone_missing"

    result = subprocess.run(
        ["rclone", "lsd", remote, "--log-level", "ERROR"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, "ok"
    return False, classify_failure(result.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check private rclone storage without leaking provider errors.")
    parser.add_argument("--remote", required=True, help="Configured rclone remote root, e.g. tl:")
    args = parser.parse_args()

    ok, code = run_preflight(args.remote)
    if ok:
        print("storage_preflight_ok")
        return 0

    print(f"storage_preflight_error code={code}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
