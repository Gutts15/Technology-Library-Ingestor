#!/usr/bin/env python3
"""Fail closed when project metadata drifts from the authoritative Charter contract."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "PROJECT_CONTRACT.json"
CHARTER_PATH = ROOT / "docs" / "PROJECT_CHARTER.md"
ROADMAP_PATH = ROOT / "docs" / "PROJECT_ROADMAP.md"
AGENTS_PATH = ROOT / "AGENTS.md"


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
    if not isinstance(completion, list) or "500-item mixed-backlog black-box acceptance pass" not in completion:
        errors.append("contract_black_box_completion_requirement_missing")

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
