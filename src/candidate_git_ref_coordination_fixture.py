#!/usr/bin/env python3
"""Exercise exact expected-ref transaction ownership using only local Git fixtures.

A dedicated Git ref is a candidate coordination primitive because a ref update can
be guarded by an exact expected old SHA. This module deliberately targets only a
local bare Git repository. It proves single-winner compare-and-swap behavior and
owner/state transitions without contacting GitHub or enabling production use.

The production question remains separate: credentials, dedicated remote ref setup,
GitHub behavior and operational recovery must pass a live integration gate before
this mechanism can satisfy candidate_remote_coordination_policy.py.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
FIXTURE_VERSION = "0.1.0"
DEFAULT_REF = "refs/heads/candidate-transaction-coordinator"
STATE_PATH = ".candidate-transaction-coordinator.json"


class GitCoordinationFailure(RuntimeError):
    pass


def git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        check=False,
        capture_output=True,
        text=True,
    )


def require_git() -> None:
    if not shutil.which("git"):
        raise GitCoordinationFailure("git_missing")


def ref_sha(remote: Path, ref: str = DEFAULT_REF) -> str | None:
    result = git(["--git-dir", str(remote), "rev-parse", "--verify", ref])
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def read_state(remote: Path, ref: str = DEFAULT_REF) -> dict[str, Any] | None:
    result = git(["--git-dir", str(remote), "show", f"{ref}:{STATE_PATH}"])
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def init_fixture(remote: Path, ref: str = DEFAULT_REF) -> tuple[str | None, list[str]]:
    try:
        require_git()
    except GitCoordinationFailure as exc:
        return None, [str(exc)]
    remote = remote.resolve()
    remote.parent.mkdir(parents=True, exist_ok=True)
    if git(["init", "--bare", str(remote)]).returncode != 0:
        return None, ["bare_init_failed"]

    with tempfile.TemporaryDirectory(prefix="git-coordination-seed-") as temp:
        work = Path(temp)
        if git(["init"], work).returncode != 0:
            return None, ["seed_init_failed"]
        git(["config", "user.name", "Technology Library Fixture"], work)
        git(["config", "user.email", "fixture@example.invalid"], work)
        state = {
            "schema_version": SCHEMA_VERSION,
            "fixture_version": FIXTURE_VERSION,
            "state": "FREE",
            "transaction_id": None,
            "owner": None,
            "canonical_write_performed": False,
        }
        (work / STATE_PATH).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        if git(["add", STATE_PATH], work).returncode != 0:
            return None, ["seed_add_failed"]
        if git(["commit", "-m", "fixture: initialize transaction coordinator"], work).returncode != 0:
            return None, ["seed_commit_failed"]
        if git(["remote", "add", "origin", str(remote)], work).returncode != 0:
            return None, ["seed_remote_failed"]
        if git(["push", "origin", f"HEAD:{ref}"], work).returncode != 0:
            return None, ["seed_push_failed"]
    sha = ref_sha(remote, ref)
    return (sha, []) if sha is not None else (None, ["seed_ref_missing"])


def prepare_state_commit(
    remote: Path,
    *,
    base_sha: str,
    state: dict[str, Any],
    ref: str = DEFAULT_REF,
) -> tuple[Path | None, str | None, list[str]]:
    """Create one child commit from an exact observed base without pushing it."""

    try:
        require_git()
    except GitCoordinationFailure as exc:
        return None, None, [str(exc)]
    temp = Path(tempfile.mkdtemp(prefix="git-coordination-owner-"))
    clone = git(["clone", "--no-checkout", str(remote.resolve()), str(temp)])
    if clone.returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["clone_failed"]
    git(["config", "user.name", "Technology Library Fixture"], temp)
    git(["config", "user.email", "fixture@example.invalid"], temp)
    fetch = git(["fetch", "origin", f"{ref}:{ref}"], temp)
    if fetch.returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["fetch_ref_failed"]
    checkout = git(["checkout", "--detach", base_sha], temp)
    if checkout.returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["base_checkout_failed"]
    payload = dict(state)
    payload["schema_version"] = SCHEMA_VERSION
    payload["fixture_version"] = FIXTURE_VERSION
    payload["canonical_write_performed"] = False
    (temp / STATE_PATH).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if git(["add", STATE_PATH], temp).returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["state_add_failed"]
    if git(["commit", "-m", f"fixture: coordinator {payload.get('state', 'UNKNOWN')}"], temp).returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["state_commit_failed"]
    head = git(["rev-parse", "HEAD"], temp)
    if head.returncode != 0 or not head.stdout.strip():
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["state_head_failed"]
    return temp, head.stdout.strip(), []


def push_expected(
    work: Path,
    remote: Path,
    *,
    expected_sha: str,
    ref: str = DEFAULT_REF,
) -> tuple[bool, str]:
    """Push only if the remote ref still equals the exact observed SHA."""

    result = git(
        [
            "push",
            f"--force-with-lease={ref}:{expected_sha}",
            str(remote.resolve()),
            f"HEAD:{ref}",
        ],
        work,
    )
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a local Git exact-ref coordination fixture.")
    parser.add_argument("--remote-dir", type=Path, required=True)
    args = parser.parse_args()

    base, errors = init_fixture(args.remote_dir)
    if errors or base is None:
        print(f"candidate_git_ref_coordination_fixture_error codes={','.join(errors or ['init_failed'])} canonical_write=0")
        return 2

    state_a = {"state": "ACTIVE", "transaction_id": "a" * 20, "owner": "owner-a"}
    state_b = {"state": "ACTIVE", "transaction_id": "b" * 20, "owner": "owner-b"}
    work_a, _, errors_a = prepare_state_commit(args.remote_dir, base_sha=base, state=state_a)
    work_b, _, errors_b = prepare_state_commit(args.remote_dir, base_sha=base, state=state_b)
    if errors_a or errors_b or work_a is None or work_b is None:
        print(f"candidate_git_ref_coordination_fixture_error codes={','.join(errors_a + errors_b or ['prepare_failed'])} canonical_write=0")
        return 2
    try:
        won_a, _ = push_expected(work_a, args.remote_dir, expected_sha=base)
        won_b, _ = push_expected(work_b, args.remote_dir, expected_sha=base)
    finally:
        shutil.rmtree(work_a, ignore_errors=True)
        shutil.rmtree(work_b, ignore_errors=True)
    if won_a == won_b:
        print("candidate_git_ref_coordination_fixture_error codes=single_winner_not_proven canonical_write=0")
        return 2
    state = read_state(args.remote_dir)
    if not isinstance(state, dict) or state.get("state") != "ACTIVE":
        print("candidate_git_ref_coordination_fixture_error codes=final_state_invalid canonical_write=0")
        return 2
    print(
        "candidate_git_ref_coordination_fixture_ok "
        f"single_winner=1 winner={state.get('owner')} exact_expected_ref=1 remote_mode=0 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
