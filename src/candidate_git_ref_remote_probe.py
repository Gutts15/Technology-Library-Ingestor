#!/usr/bin/env python3
"""Probe remote Git exact-ref CAS plus restart/recovery on a disposable ref.

This integration probe is intentionally unable to publish candidate records. It
writes only a randomly generated branch named ``tl-coordination-fixture-<random>``
and stores synthetic coordinator state in one file on that disposable branch.
It never targets main/master and never reads or writes 00_LIBRARY.

The probe proves two separate properties needed by a future coordinator design:

1. two contenders racing from the same observed ref produce exactly one winner;
2. coordinator state survives a fresh process/read, an explicitly stale fixture
   owner can be recovered with exact-ref CAS, the pre-recovery owner cannot later
   overwrite recovered state with its stale lease, and the new owner can acquire
   and release normally.

No remote write occurs unless ``--apply-remote-fixture`` is supplied explicitly.
Passing this probe is evidence for coordination mechanics only. It does not
authorize production publication and it does not implement the production
candidate executor.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
PROBE_VERSION = "0.2.0"
STATE_PATH = ".candidate-transaction-coordinator.json"
REF_PREFIX = "refs/heads/tl-coordination-fixture-"


def git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        check=False,
        capture_output=True,
        text=True,
    )


def ls_remote(remote_url: str, ref: str) -> str | None:
    result = git(["ls-remote", remote_url, ref])
    if result.returncode != 0:
        return None
    line = result.stdout.strip()
    if not line:
        return ""
    return line.split()[0] if line.split() else ""


def _configure(work: Path) -> None:
    git(["config", "user.name", "Technology Library Coordination Probe"], work)
    git(["config", "user.email", "coordination-probe@example.invalid"], work)


def _state_payload(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "probe_version": PROBE_VERSION,
        **state,
        "fixture_only": True,
        "canonical_write_performed": False,
    }


def _write_state(work: Path, state: dict[str, Any]) -> None:
    (work / STATE_PATH).write_text(
        json.dumps(_state_payload(state), indent=2) + "\n",
        encoding="utf-8",
    )


def _safe_fixture_ref(ref: str) -> bool:
    return ref.startswith(REF_PREFIX) and len(ref) > len(REF_PREFIX)


def seed_ref(remote_url: str, ref: str) -> tuple[str | None, list[str]]:
    if not _safe_fixture_ref(ref):
        return None, ["unsafe_fixture_ref"]
    before = ls_remote(remote_url, ref)
    if before is None:
        return None, ["remote_unreachable"]
    if before:
        return None, ["fixture_ref_already_exists"]

    with tempfile.TemporaryDirectory(prefix="tl-coordination-remote-seed-") as temp:
        work = Path(temp)
        if git(["init"], work).returncode != 0:
            return None, ["seed_init_failed"]
        _configure(work)
        _write_state(
            work,
            {
                "state": "FREE",
                "transaction_id": None,
                "owner": None,
                "generation": 0,
                "lease_expired_fixture": False,
                "recovery_verified": False,
                "recovered_from_owner": None,
            },
        )
        if git(["add", STATE_PATH], work).returncode != 0:
            return None, ["seed_add_failed"]
        if git(["commit", "-m", "fixture: initialize remote coordination probe"], work).returncode != 0:
            return None, ["seed_commit_failed"]
        head = git(["rev-parse", "HEAD"], work)
        if head.returncode != 0 or not head.stdout.strip():
            return None, ["seed_head_failed"]
        pushed = git(["push", remote_url, f"HEAD:{ref}"], work)
        if pushed.returncode != 0:
            return None, ["seed_push_conflict_or_failed"]
        expected = head.stdout.strip()
    observed = ls_remote(remote_url, ref)
    if observed != expected:
        return None, ["seed_verify_failed"]
    return expected, []


def _prepare_state_commit(
    remote_url: str,
    ref: str,
    base_sha: str,
    state: dict[str, Any],
    *,
    message: str,
) -> tuple[Path | None, str | None, list[str]]:
    if not _safe_fixture_ref(ref):
        return None, None, ["unsafe_fixture_ref"]
    temp = Path(tempfile.mkdtemp(prefix="tl-coordination-remote-state-"))
    if git(["init"], temp).returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["state_init_failed"]
    _configure(temp)
    if git(["remote", "add", "origin", remote_url], temp).returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["state_remote_failed"]
    fetch = git(["fetch", "--no-tags", "origin", ref], temp)
    if fetch.returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["state_fetch_failed"]
    observed = git(["rev-parse", "FETCH_HEAD"], temp)
    if observed.returncode != 0 or observed.stdout.strip() != base_sha:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["state_base_changed"]
    if git(["checkout", "--detach", base_sha], temp).returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["state_checkout_failed"]
    _write_state(temp, state)
    if git(["add", STATE_PATH], temp).returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["state_add_failed"]
    if git(["commit", "-m", message], temp).returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["state_commit_failed"]
    head = git(["rev-parse", "HEAD"], temp)
    if head.returncode != 0 or not head.stdout.strip():
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["state_head_failed"]
    return temp, head.stdout.strip(), []


def prepare_contender(
    remote_url: str,
    ref: str,
    base_sha: str,
    *,
    transaction_id: str,
    owner: str,
) -> tuple[Path | None, str | None, list[str]]:
    return _prepare_state_commit(
        remote_url,
        ref,
        base_sha,
        {
            "state": "ACTIVE",
            "transaction_id": transaction_id,
            "owner": owner,
            "generation": 1,
            # Explicit fixture marker. Production staleness must be established by
            # the future coordinator's real lease/timeout policy, not by this flag.
            "lease_expired_fixture": True,
            "recovery_verified": False,
            "recovered_from_owner": None,
        },
        message=f"fixture: acquire {owner}",
    )


def push_expected(work: Path, ref: str, expected_sha: str) -> tuple[bool, str]:
    result = git(
        [
            "push",
            f"--force-with-lease={ref}:{expected_sha}",
            "origin",
            f"HEAD:{ref}",
        ],
        work,
    )
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def read_ref_state(remote_url: str, ref: str) -> tuple[str | None, dict[str, Any] | None, list[str]]:
    """Read coordinator state through a fresh temporary Git process/worktree."""

    if not _safe_fixture_ref(ref):
        return None, None, ["unsafe_fixture_ref"]
    observed = ls_remote(remote_url, ref)
    if observed is None:
        return None, None, ["remote_unreachable"]
    if not observed:
        return "", None, ["fixture_ref_missing"]

    with tempfile.TemporaryDirectory(prefix="tl-coordination-remote-read-") as temp:
        work = Path(temp)
        if git(["init"], work).returncode != 0:
            return None, None, ["read_init_failed"]
        if git(["remote", "add", "origin", remote_url], work).returncode != 0:
            return None, None, ["read_remote_failed"]
        fetch = git(["fetch", "--no-tags", "origin", ref], work)
        if fetch.returncode != 0:
            return None, None, ["read_fetch_failed"]
        fetched = git(["rev-parse", "FETCH_HEAD"], work)
        if fetched.returncode != 0 or fetched.stdout.strip() != observed:
            return None, None, ["read_sha_changed"]
        shown = git(["show", f"FETCH_HEAD:{STATE_PATH}"], work)
        if shown.returncode != 0:
            return None, None, ["read_state_missing"]
        try:
            payload = json.loads(shown.stdout)
        except json.JSONDecodeError:
            return None, None, ["read_state_invalid_json"]
    if not isinstance(payload, dict):
        return None, None, ["read_state_invalid"]
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("fixture_only") is not True:
        return None, None, ["read_state_contract"]
    if payload.get("canonical_write_performed") is not False:
        return None, None, ["read_state_write_flag"]
    return observed, payload, []


def prepare_verified_recovery(
    remote_url: str,
    ref: str,
) -> tuple[Path | None, str | None, str | None, str | None, list[str]]:
    """Prepare recovery only after a fresh read proves the fixture lease stale."""

    current_sha, current, errors = read_ref_state(remote_url, ref)
    if errors or current_sha is None or current is None:
        return None, None, None, None, errors or ["recovery_read_failed"]
    if current.get("state") != "ACTIVE":
        return None, None, None, None, ["recovery_requires_active"]
    owner = current.get("owner")
    transaction_id = current.get("transaction_id")
    if not isinstance(owner, str) or not owner:
        return None, None, None, None, ["recovery_owner_missing"]
    if not isinstance(transaction_id, str) or not transaction_id:
        return None, None, None, None, ["recovery_transaction_missing"]
    if current.get("lease_expired_fixture") is not True:
        return None, None, None, None, ["recovery_staleness_not_verified"]

    work, head, prep_errors = _prepare_state_commit(
        remote_url,
        ref,
        current_sha,
        {
            "state": "RECOVERED",
            "transaction_id": None,
            "owner": None,
            "generation": int(current.get("generation") or 0) + 1,
            "lease_expired_fixture": False,
            "recovery_verified": True,
            "recovered_from_owner": owner,
            "recovered_from_transaction_id": transaction_id,
        },
        message=f"fixture: verified recovery from {owner}",
    )
    return work, head, current_sha, owner, prep_errors


def prepare_owned_release(
    remote_url: str,
    ref: str,
    base_sha: str,
    *,
    owner: str,
    transaction_id: str,
    generation: int,
) -> tuple[Path | None, str | None, list[str]]:
    return _prepare_state_commit(
        remote_url,
        ref,
        base_sha,
        {
            "state": "FREE",
            "transaction_id": None,
            "owner": None,
            "generation": generation,
            "lease_expired_fixture": False,
            "recovery_verified": False,
            "recovered_from_owner": None,
            "released_by_owner": owner,
            "released_transaction_id": transaction_id,
        },
        message=f"fixture: release {owner}",
    )


def prepare_restarted_acquire(
    remote_url: str,
    ref: str,
    base_sha: str,
) -> tuple[Path | None, str | None, list[str]]:
    return _prepare_state_commit(
        remote_url,
        ref,
        base_sha,
        {
            "state": "ACTIVE",
            "transaction_id": "c" * 20,
            "owner": "probe-owner-restarted",
            "generation": 3,
            "lease_expired_fixture": False,
            "recovery_verified": False,
            "recovered_from_owner": None,
        },
        message="fixture: acquire after verified recovery",
    )


def cleanup_ref(remote_url: str, ref: str, expected_sha: str) -> list[str]:
    if not _safe_fixture_ref(ref):
        return ["unsafe_fixture_ref"]
    with tempfile.TemporaryDirectory(prefix="tl-coordination-remote-cleanup-") as temp:
        work = Path(temp)
        if git(["init"], work).returncode != 0:
            return ["cleanup_init_failed"]
        result = git(
            [
                "push",
                f"--force-with-lease={ref}:{expected_sha}",
                remote_url,
                f":{ref}",
            ],
            work,
        )
        if result.returncode != 0:
            return ["cleanup_push_failed"]
    observed = ls_remote(remote_url, ref)
    if observed is None:
        return ["cleanup_remote_unreachable"]
    if observed:
        return ["cleanup_verify_failed"]
    return []


def _cleanup_current(remote_url: str, ref: str) -> list[str]:
    current = ls_remote(remote_url, ref)
    if current is None:
        return ["cleanup_remote_unreachable"]
    if not current:
        return []
    return cleanup_ref(remote_url, ref, current)


def run_probe(remote_url: str) -> tuple[dict[str, Any] | None, list[str]]:
    if not shutil.which("git"):
        return None, ["git_missing"]
    ref = REF_PREFIX + uuid.uuid4().hex[:12]
    base_sha, seed_errors = seed_ref(remote_url, ref)
    if seed_errors or base_sha is None:
        return {"state": "SEED_FAILED", "fixture_ref": ref, "canonical_write_performed": False}, seed_errors

    work_a, head_a, errors_a = prepare_contender(
        remote_url,
        ref,
        base_sha,
        transaction_id="a" * 20,
        owner="probe-owner-a",
    )
    work_b, head_b, errors_b = prepare_contender(
        remote_url,
        ref,
        base_sha,
        transaction_id="b" * 20,
        owner="probe-owner-b",
    )
    if errors_a or errors_b or work_a is None or work_b is None or head_a is None or head_b is None:
        cleanup_errors = _cleanup_current(remote_url, ref)
        return {
            "state": "PREPARE_FAILED",
            "fixture_ref": ref,
            "cleanup_required": bool(cleanup_errors),
            "canonical_write_performed": False,
        }, [*errors_a, *errors_b, *cleanup_errors]

    try:
        won_a, message_a = push_expected(work_a, ref, base_sha)
        won_b, message_b = push_expected(work_b, ref, base_sha)
    finally:
        shutil.rmtree(work_a, ignore_errors=True)
        shutil.rmtree(work_b, ignore_errors=True)

    current = ls_remote(remote_url, ref)
    if current is None:
        return {
            "state": "VERIFY_FAILED",
            "fixture_ref": ref,
            "cleanup_required": True,
            "canonical_write_performed": False,
        }, ["winner_read_failed"]
    single_winner = won_a != won_b and current in {head_a, head_b}
    winner = "probe-owner-a" if won_a and not won_b else "probe-owner-b" if won_b and not won_a else None
    winner_tx = "a" * 20 if winner == "probe-owner-a" else "b" * 20 if winner == "probe-owner-b" else None
    errors: list[str] = []
    if not single_winner or winner is None or winner_tx is None:
        errors.append("single_winner_not_proven")

    fresh_sha, fresh_state, fresh_errors = read_ref_state(remote_url, ref)
    errors.extend(fresh_errors)
    restart_read = (
        not fresh_errors
        and fresh_sha == current
        and isinstance(fresh_state, dict)
        and fresh_state.get("state") == "ACTIVE"
        and fresh_state.get("owner") == winner
        and fresh_state.get("transaction_id") == winner_tx
    )
    if not restart_read:
        errors.append("restart_state_not_proven")

    # Prepare the original owner's release while its lease is still current. We
    # intentionally delay the push until after recovery, proving the old lease can
    # no longer overwrite the recovered state.
    stale_release_work: Path | None = None
    stale_release_head: str | None = None
    if not errors and current:
        stale_release_work, stale_release_head, stale_release_errors = prepare_owned_release(
            remote_url,
            ref,
            current,
            owner=winner,
            transaction_id=winner_tx,
            generation=2,
        )
        errors.extend(stale_release_errors)

    recovery_work: Path | None = None
    recovery_head: str | None = None
    recovery_base: str | None = None
    recovered_from_owner: str | None = None
    if not errors:
        recovery_work, recovery_head, recovery_base, recovered_from_owner, recovery_errors = prepare_verified_recovery(
            remote_url,
            ref,
        )
        errors.extend(recovery_errors)

    recovery_pushed = False
    if not errors and recovery_work is not None and recovery_head is not None and recovery_base is not None:
        recovery_pushed, _ = push_expected(recovery_work, ref, recovery_base)
        if not recovery_pushed:
            errors.append("recovery_push_failed")
    if recovery_work is not None:
        shutil.rmtree(recovery_work, ignore_errors=True)

    recovered_sha, recovered_state, recovered_read_errors = read_ref_state(remote_url, ref) if recovery_pushed else (None, None, [])
    errors.extend(recovered_read_errors)
    recovery_verified = (
        recovery_pushed
        and recovered_sha == recovery_head
        and isinstance(recovered_state, dict)
        and recovered_state.get("state") == "RECOVERED"
        and recovered_state.get("recovery_verified") is True
        and recovered_state.get("recovered_from_owner") == recovered_from_owner == winner
    )
    if recovery_pushed and not recovery_verified:
        errors.append("recovery_verify_failed")

    stale_owner_blocked = False
    stale_owner_message = ""
    if recovery_verified and stale_release_work is not None and stale_release_head is not None and current:
        stale_won, stale_owner_message = push_expected(stale_release_work, ref, current)
        stale_owner_blocked = not stale_won and ls_remote(remote_url, ref) == recovered_sha
        if not stale_owner_blocked:
            errors.append("stale_owner_overwrite_not_blocked")
    if stale_release_work is not None:
        shutil.rmtree(stale_release_work, ignore_errors=True)

    restarted_acquire_work: Path | None = None
    restarted_acquire_head: str | None = None
    if not errors and isinstance(recovered_sha, str) and recovered_sha:
        restarted_acquire_work, restarted_acquire_head, acquire_errors = prepare_restarted_acquire(
            remote_url,
            ref,
            recovered_sha,
        )
        errors.extend(acquire_errors)

    restarted_acquired = False
    if not errors and restarted_acquire_work is not None and restarted_acquire_head is not None and recovered_sha:
        restarted_acquired, _ = push_expected(restarted_acquire_work, ref, recovered_sha)
        if not restarted_acquired:
            errors.append("restart_acquire_failed")
    if restarted_acquire_work is not None:
        shutil.rmtree(restarted_acquire_work, ignore_errors=True)

    acquired_sha, acquired_state, acquired_errors = read_ref_state(remote_url, ref) if restarted_acquired else (None, None, [])
    errors.extend(acquired_errors)
    if restarted_acquired and not (
        acquired_sha == restarted_acquire_head
        and isinstance(acquired_state, dict)
        and acquired_state.get("state") == "ACTIVE"
        and acquired_state.get("owner") == "probe-owner-restarted"
        and acquired_state.get("transaction_id") == "c" * 20
    ):
        errors.append("restart_acquire_verify_failed")

    release_work: Path | None = None
    release_head: str | None = None
    if not errors and isinstance(acquired_sha, str) and acquired_sha:
        release_work, release_head, release_errors = prepare_owned_release(
            remote_url,
            ref,
            acquired_sha,
            owner="probe-owner-restarted",
            transaction_id="c" * 20,
            generation=4,
        )
        errors.extend(release_errors)

    owner_release_pushed = False
    if not errors and release_work is not None and release_head is not None and acquired_sha:
        owner_release_pushed, _ = push_expected(release_work, ref, acquired_sha)
        if not owner_release_pushed:
            errors.append("owner_release_failed")
    if release_work is not None:
        shutil.rmtree(release_work, ignore_errors=True)

    released_sha, released_state, released_errors = read_ref_state(remote_url, ref) if owner_release_pushed else (None, None, [])
    errors.extend(released_errors)
    owner_release_verified = (
        owner_release_pushed
        and released_sha == release_head
        and isinstance(released_state, dict)
        and released_state.get("state") == "FREE"
        and released_state.get("owner") is None
        and released_state.get("released_by_owner") == "probe-owner-restarted"
    )
    if owner_release_pushed and not owner_release_verified:
        errors.append("owner_release_verify_failed")

    cleanup_errors = cleanup_ref(remote_url, ref, released_sha) if isinstance(released_sha, str) and released_sha else _cleanup_current(remote_url, ref)
    errors.extend(cleanup_errors)

    report = {
        "schema_version": SCHEMA_VERSION,
        "probe_version": PROBE_VERSION,
        "state": "PASS" if not errors else "FAIL",
        "fixture_ref": ref,
        "single_winner": single_winner,
        "winner": winner,
        "contender_a_succeeded": won_a,
        "contender_b_succeeded": won_b,
        "contender_a_message": message_a[-1000:],
        "contender_b_message": message_b[-1000:],
        "restart_state_read": restart_read,
        "recovery_verified": recovery_verified,
        "recovered_from_owner": recovered_from_owner,
        "stale_owner_blocked": stale_owner_blocked,
        "stale_owner_message": stale_owner_message[-1000:],
        "restarted_owner_acquired": restarted_acquired,
        "owner_release_verified": owner_release_verified,
        "cleanup_verified": not cleanup_errors,
        "cleanup_required": bool(cleanup_errors),
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }
    return report, sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe disposable remote Git CAS and restart/recovery behavior.")
    parser.add_argument("--remote-url", required=True)
    parser.add_argument("--apply-remote-fixture", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if not args.apply_remote_fixture:
        print(
            "candidate_git_ref_remote_probe_ok mode=plan remote_write=0 "
            f"probe_version={PROBE_VERSION} ref_prefix={REF_PREFIX} authorized=0 canonical_write=0"
        )
        return 0

    report, errors = run_probe(args.remote_url)
    if args.out is not None and report is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if errors or report is None:
        ref = report.get("fixture_ref") if isinstance(report, dict) else "unknown"
        cleanup = 1 if isinstance(report, dict) and report.get("cleanup_required") else 0
        print(
            "candidate_git_ref_remote_probe_error "
            f"codes={','.join(errors or ['probe_failed'])} fixture_ref={ref} cleanup_required={cleanup} "
            "authorized=0 canonical_write=0"
        )
        return 2
    print(
        "candidate_git_ref_remote_probe_ok "
        f"mode=apply_fixture state={report['state']} single_winner=1 restart_read=1 "
        "recovery_verified=1 stale_owner_blocked=1 owner_release_verified=1 "
        "cleanup_verified=1 authorized=0 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
