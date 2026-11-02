#!/usr/bin/env python3
"""Fail closed when project metadata drifts from the authoritative Charter contract."""

from __future__ import annotations

import json
from datetime import date, timedelta
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "PROJECT_CONTRACT.json"
CHARTER_PATH = ROOT / "docs" / "PROJECT_CHARTER.md"
ROADMAP_PATH = ROOT / "docs" / "PROJECT_ROADMAP.md"
AGENTS_PATH = ROOT / "AGENTS.md"

V1_REQUIRED_CASES = {
    "empty_day", "create_and_routed_retrieval",
    "update_preserves_identity_and_valid_content", "duplicate_unchanged_without_reanalysis",
    "irrelevant_reject", "weak_evidence_hold", "failure_and_next_cycle_recovery",
    "credential_lifetime_without_manual_renewal", "public_logs_and_artifacts_private",
}


def _validate_v1_acceptance(contract: dict) -> list[str]:
    """Validate the approved policy and sanitized proof; never invent runtime evidence.

    This metadata gate cannot independently authenticate private receipts. Reviewers
    must verify the private evidence manifest before setting acceptance to PASS.
    """
    errors = []
    revision = contract.get("scope_revision")
    if not isinstance(revision, dict):
        errors.append("scope_revision_missing")
    else:
        if not isinstance(revision.get("owner_approval"), str) or not revision["owner_approval"].strip():
            errors.append("scope_revision_owner_approval_missing")
        if (revision.get("from_charter_version") != "1.0"
                or revision.get("to_charter_version") != "1.1"
                or revision.get("scale_test") != "OUTSIDE_V1"):
            errors.append("scope_revision_invalid")
    policy = contract.get("v1_acceptance")
    if not isinstance(policy, dict):
        return errors + ["v1_acceptance_missing"]
    expected = {"specification_version": "1.2.0", "real_execution_required": True,
                "consecutive_days": 7, "drive_credential_age_days_exclusive_minimum": 8,
                "scale_test_required": False}
    for key, value in expected.items():
        if type(policy.get(key)) is not type(value) or policy.get(key) != value:
            errors.append("v1_acceptance_" + key)
    cases = policy.get("required_cases")
    if (not isinstance(cases, list) or any(not isinstance(x, str) for x in cases)
            or len(cases) != len(V1_REQUIRED_CASES) or set(cases) != V1_REQUIRED_CASES):
        errors.append("v1_acceptance_required_cases_mismatch")
    if str(contract.get("black_box_acceptance_status", "")).upper() != "PASS":
        return errors
    proof = contract.get("acceptance_evidence")
    if not isinstance(proof, dict):
        return errors + ["acceptance_evidence_missing"]
    if proof.get("real_execution") is not True:
        errors.append("acceptance_not_real_execution")
    try:
        days = [date.fromisoformat(value) for value in proof["scheduled_days_utc"]]
        if len(days) < 7 or any(b != a + timedelta(days=1) for a, b in zip(days, days[1:])):
            errors.append("acceptance_days_not_consecutive")
    except (KeyError, TypeError, ValueError):
        errors.append("acceptance_days_not_consecutive")
    passed = proof.get("cases_passed")
    if (not isinstance(passed, list) or any(not isinstance(x, str) for x in passed)
            or set(passed) != V1_REQUIRED_CASES):
        errors.append("acceptance_cases_incomplete")
    age = proof.get("drive_credential_age_days_without_manual_renewal")
    if type(age) not in (int, float) or not 8 < age < float("inf"):
        errors.append("acceptance_credential_age_insufficient")
    digest = proof.get("private_evidence_manifest_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        errors.append("acceptance_evidence_digest_invalid")
    return errors


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _marker(text: str, key: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*([^\n]+)\s*$", text)
    return match.group(1).strip() if match else None


def validate(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    paths = {
        "contract": root / "docs" / "PROJECT_CONTRACT.json",
        "charter": root / "docs" / "PROJECT_CHARTER.md",
        "roadmap": root / "docs" / "PROJECT_ROADMAP.md",
        "agents": root / "AGENTS.md",
    }
    for name, path in paths.items():
        if not path.is_file():
            errors.append(f"missing_{name}")
    if errors:
        return sorted(errors)

    try:
        contract = json.loads(_read(paths["contract"]))
    except (OSError, json.JSONDecodeError):
        return ["contract_invalid_json"]

    charter = _read(paths["charter"])
    roadmap = _read(paths["roadmap"])
    agents = _read(paths["agents"])

    required_top = {
        "project",
        "charter_version",
        "project_status",
        "north_star",
        "black_box_acceptance_status",
        "hard_constraints",
        "normal_flow_human_actions_allowed",
        "completion_requires",
    }
    for key in sorted(required_top):
        if key not in contract:
            errors.append(f"contract_missing_{key}")

    version = str(contract.get("charter_version", "")).strip()
    if not version:
        errors.append("contract_charter_version_empty")
    for name, text in (("charter", charter), ("roadmap", roadmap)):
        if _marker(text, "CHARTER_VERSION") != version:
            errors.append(f"{name}_charter_version_mismatch")

    expected_constraints = {
        "mandatory_additional_recurring_cost_brl": 0,
        "user_pc_required_for_normal_operation": False,
        "manual_content_review_required": False,
        "manual_candidate_review_required": False,
        "manual_batch_approval_required": False,
        "manual_canonical_approval_required": False,
        "automatic_canonical_promotion_required": True,
        "private_user_content_must_remain_private": True,
        "public_code_repository_target_after_security_audit": True,
    }
    hard = contract.get("hard_constraints")
    if not isinstance(hard, dict):
        errors.append("contract_hard_constraints_invalid")
    else:
        for key, expected in expected_constraints.items():
            if hard.get(key) != expected:
                errors.append(f"hard_constraint_{key}")

    if contract.get("normal_flow_human_actions_allowed") != []:
        errors.append("normal_flow_human_actions_not_empty")

    project_status = str(contract.get("project_status", "")).upper()
    black_box = str(contract.get("black_box_acceptance_status", "")).upper()
    if project_status == "COMPLETE" and black_box != "PASS":
        errors.append("complete_without_black_box_pass")

    roadmap_status = _marker(roadmap, "PROJECT_STATUS")
    if roadmap_status and roadmap_status.upper() != project_status:
        errors.append("roadmap_project_status_mismatch")

    if project_status != "COMPLETE":
        if re.search(r"(?im)^PROJECT_STATUS:\s*COMPLETE\s*$", roadmap):
            errors.append("roadmap_claims_project_complete")

    if (root / "docs" / "CURRENT_STATE.md").exists():
        errors.append("private_current_state_present")

    charter_required_phrases = [
        "Routine ingestion and knowledge promotion must not require",
        "The project is not complete until this black-box test passes.",
        "HELD is a machine state, not a review queue the user is expected to clear.",
        "Ordinary canonical promotion is policy-driven and automatic",
    ]
    for index, phrase in enumerate(charter_required_phrases, start=1):
        if phrase not in charter:
            errors.append(f"charter_invariant_phrase_{index}")

    if "docs/PROJECT_CHARTER.md" not in agents or "docs/PROJECT_CONTRACT.json" not in agents:
        errors.append("agents_missing_charter_contract_refs")
    if "Do not infer a scope change from approval of a technical step." not in agents:
        errors.append("agents_scope_change_guard_missing")

    completion = contract.get("completion_requires")
    if (not isinstance(completion, list)
            or "seven consecutive real scheduled days black-box acceptance pass" not in completion):
        errors.append("contract_black_box_completion_requirement_missing")
    if version != "1.1":
        errors.append("contract_unapproved_charter_version")
    errors.extend(_validate_v1_acceptance(contract))

    return sorted(set(errors))


def main() -> int:
    errors = validate()
    if errors:
        print(
            "project_alignment_guard_error "
            f"codes={','.join(errors)} alignment=FAIL"
        )
        return 2

    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    print(
        "project_alignment_guard_ok "
        f"alignment=PASS charter_version={contract['charter_version']} "
        f"project_status={contract['project_status']} "
        f"black_box={contract['black_box_acceptance_status']} "
        "routine_human_steps=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
