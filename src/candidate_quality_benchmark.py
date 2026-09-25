#!/usr/bin/env python3
"""Run a local quality benchmark over selected private chat candidates.

The benchmark exists to answer one question before more automation is enabled:
can the configured loopback-only Ollama model make useful, conservative semantic
reviews on real Technology Library candidates?

It reads candidates through rclone, reuses the production semantic review code,
and optionally exercises SPLIT_REQUIRED planning. Results are written only to a
local git-ignored report path. It never moves candidates, never writes to Drive,
never writes to 00_LIBRARY, and never uses a paid/remote model endpoint.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_semantic_batch import index_map, proposal_map, remote_json, remote_text, review_one
from candidate_semantic_local import endpoint_is_loopback
from candidate_split_plan import (
    build_prompt as build_split_prompt,
    build_split_plan,
    call_ollama as call_split_ollama,
)

BENCHMARK_VERSION = "0.1.0"
SCHEMA_VERSION = 1
DEFAULT_ROOT = "99_INBOX/CANDIDATES"
DEFAULT_MASTER = "00_LIBRARY/MASTER_INDEX.md"
DEFAULT_OUT = Path("output/candidate-quality-benchmark.json")


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["rclone", *arguments], check=False, capture_output=True)


def basename(value: str) -> str:
    return PurePosixPath(value).name


def resolve_selected(
    index: dict[str, Any] | None,
    proposals: dict[str, Any] | None,
    requested: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    current = index_map(index)
    proposed = proposal_map(proposals)
    by_name: dict[str, dict[str, Any]] = {}
    for candidate_id, entry in current.items():
        path = entry.get("path")
        if isinstance(path, str):
            by_name[basename(path)] = {"candidate_id": candidate_id, **entry}

    selected: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for raw in requested:
        name = basename(raw)
        if name in seen:
            continue
        seen.add(name)
        entry = by_name.get(name)
        if entry is None:
            errors.append(f"candidate_not_indexed:{name}")
            continue
        candidate_id = entry["candidate_id"]
        proposal = proposed.get(candidate_id)
        if not isinstance(proposal, dict) or proposal.get("proposal") != "READY_FOR_SEMANTIC":
            errors.append(f"candidate_not_ready_for_semantic:{name}")
            continue
        selected.append(entry)
    return selected, errors


def quality_checks(
    *,
    candidate_name: str,
    decision: dict[str, Any],
    rich_review: dict[str, Any],
    expect_split: bool,
    split_plan: dict[str, Any] | None,
    split_errors: list[str],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    final = rich_review.get("final_review") if isinstance(rich_review, dict) else None
    actual = decision.get("decision") if isinstance(decision, dict) else None
    if not isinstance(final, dict) or final.get("decision") != actual:
        reasons.append("decision_mismatch")
    if rich_review.get("canonical_write_performed") is not False:
        reasons.append("unsafe_canonical_write_flag")
    if actual == "NEEDS_REVIEW":
        reasons.append("semantic_fail_closed")
    if expect_split and actual != "SPLIT_REQUIRED":
        reasons.append("expected_split_not_detected")
    if actual == "SPLIT_REQUIRED":
        if split_errors:
            reasons.append("split_plan_invalid")
        if not isinstance(split_plan, dict):
            reasons.append("split_plan_missing")
        else:
            child_count = split_plan.get("child_count")
            if not isinstance(child_count, int) or not (2 <= child_count <= 8):
                reasons.append("split_child_count_invalid")
            if split_plan.get("candidate_write_performed") is not False:
                reasons.append("unsafe_split_candidate_write_flag")
            if split_plan.get("canonical_write_performed") is not False:
                reasons.append("unsafe_split_canonical_write_flag")
            if split_plan.get("paid_model_used") is not False:
                reasons.append("unsafe_split_paid_model_flag")
    return not reasons, reasons


def compact_item(
    *,
    name: str,
    entry: dict[str, Any],
    rich: dict[str, Any],
    decision: dict[str, Any],
    split_plan: dict[str, Any] | None,
    split_errors: list[str],
    passed: bool,
    reasons: list[str],
) -> dict[str, Any]:
    source_probe = rich.get("source_probe") if isinstance(rich.get("source_probe"), dict) else {}
    result: dict[str, Any] = {
        "candidate_name": name,
        "candidate_id": entry.get("candidate_id"),
        "candidate_sha256": entry.get("sha256"),
        "decision": decision.get("decision"),
        "evidence_level": decision.get("evidence_level"),
        "policy_reasons": list(rich.get("policy_reasons") or []),
        "source_probe": {
            "source_count": int(source_probe.get("source_count", 0) or 0),
            "successful_sources": int(source_probe.get("successful_sources", 0) or 0),
            "blocked_sources": int(source_probe.get("blocked_sources", 0) or 0),
            "failed_sources": int(source_probe.get("failed_sources", 0) or 0),
        },
        "split": None,
        "quality_pass": passed,
        "quality_reasons": reasons,
        "candidate_write_performed": False,
        "curation_transition_performed": False,
        "canonical_write_performed": False,
        "paid_model_used": False,
    }
    if split_plan is not None or split_errors:
        children = []
        if isinstance(split_plan, dict):
            for child in split_plan.get("children") or []:
                if isinstance(child, dict):
                    children.append(
                        {
                            "title": child.get("title"),
                            "proposed_type": child.get("proposed_type"),
                            "proposed_status": child.get("proposed_status"),
                            "proposed_domain": child.get("proposed_domain"),
                            "proposed_category": child.get("proposed_category"),
                            "source_indices": child.get("source_indices"),
                        }
                    )
        result["split"] = {
            "errors": split_errors,
            "child_count": split_plan.get("child_count") if isinstance(split_plan, dict) else 0,
            "children": children,
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark local semantic quality on selected private chat candidates.")
    parser.add_argument("--remote", default="tl:")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--model", required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--candidate", action="append", required=True, help="Candidate filename; repeat for multiple candidates.")
    parser.add_argument("--expect-split", action="append", default=[], help="Candidate filename expected to resolve SPLIT_REQUIRED.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("candidate_quality_benchmark_error code=rclone_missing canonical_write=0 paid_model=0")
        return 2
    if not endpoint_is_loopback(args.endpoint):
        print("candidate_quality_benchmark_error code=non_loopback_model_endpoint canonical_write=0 paid_model=0")
        return 2

    index = remote_json(args.remote, str(PurePosixPath(args.root) / "index.json"))
    proposals = remote_json(args.remote, str(PurePosixPath(args.root) / "validation-proposals.json"))
    master_text = remote_text(args.remote, DEFAULT_MASTER)
    if index is None or proposals is None or master_text is None:
        print("candidate_quality_benchmark_error code=private_state_missing canonical_write=0 paid_model=0")
        return 2

    entries, selection_errors = resolve_selected(index, proposals, list(args.candidate))
    if selection_errors or len(entries) != len({basename(value) for value in args.candidate}):
        print(f"candidate_quality_benchmark_error code=selection_failed count={len(selection_errors)} canonical_write=0 paid_model=0")
        return 2

    expected_split = {basename(value) for value in args.expect_split}
    results: list[dict[str, Any]] = []
    passed_count = 0
    fail_closed_count = 0

    for entry in entries:
        path = entry.get("path")
        candidate_id = entry.get("candidate_id")
        candidate_sha = entry.get("sha256")
        if not all(isinstance(value, str) and value for value in (path, candidate_id, candidate_sha)):
            print("candidate_quality_benchmark_error code=index_entry_invalid canonical_write=0 paid_model=0")
            return 2
        name = basename(path)
        candidate_text = remote_text(args.remote, str(PurePosixPath(args.root) / path))
        if candidate_text is None:
            print("candidate_quality_benchmark_error code=candidate_read_failed canonical_write=0 paid_model=0")
            return 2

        rich, decision = review_one(
            candidate_id=candidate_id,
            candidate_sha=candidate_sha,
            candidate_text=candidate_text,
            master_text=master_text,
            model=args.model,
            endpoint=args.endpoint,
            timeout=max(10.0, min(float(args.timeout), 600.0)),
        )

        split_plan = None
        split_errors: list[str] = []
        if decision.get("decision") == "SPLIT_REQUIRED":
            probe = rich.get("source_probe") if isinstance(rich, dict) else None
            if not isinstance(probe, dict):
                split_errors = ["source_probe_missing"]
            else:
                payload = call_split_ollama(
                    args.endpoint,
                    args.model,
                    build_split_prompt(candidate_text, rich, probe),
                    max(10.0, min(float(args.timeout), 600.0)),
                )
                if payload is None:
                    split_errors = ["local_split_model_failed"]
                else:
                    split_plan, split_errors = build_split_plan(candidate_text, rich, payload)

        passed, reasons = quality_checks(
            candidate_name=name,
            decision=decision,
            rich_review=rich,
            expect_split=name in expected_split,
            split_plan=split_plan,
            split_errors=split_errors,
        )
        if passed:
            passed_count += 1
        if decision.get("decision") == "NEEDS_REVIEW":
            fail_closed_count += 1
        results.append(
            compact_item(
                name=name,
                entry=entry,
                rich=rich,
                decision=decision,
                split_plan=split_plan,
                split_errors=split_errors,
                passed=passed,
                reasons=reasons,
            )
        )

    report = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_version": BENCHMARK_VERSION,
        "model": args.model,
        "endpoint_scope": "loopback_only",
        "selected": len(results),
        "passed": passed_count,
        "failed": len(results) - passed_count,
        "fail_closed": fail_closed_count,
        "overall_pass": passed_count == len(results),
        "items": results,
        "candidate_write_performed": False,
        "curation_transition_performed": False,
        "canonical_write_performed": False,
        "paid_model_used": False,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_quality_benchmark_ok "
        f"selected={len(results)} passed={passed_count} failed={len(results)-passed_count} "
        f"fail_closed={fail_closed_count} overall_pass={int(report['overall_pass'])} "
        "candidate_write=0 curation_transition=0 canonical_write=0 paid_model=0"
    )
    return 0 if report["overall_pass"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
