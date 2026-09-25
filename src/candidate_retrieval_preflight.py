#!/usr/bin/env python3
"""Build a read-only virtual post-candidate catalog and run structural retrieval checks.

This stage does not simulate ChatGPT retrieval quality. Instead it proves the part
that can be checked deterministically before publication: the sealed candidate
batch can be overlaid on the current canonical raw records without duplicate
ownership/title conflicts, generated indexes remain buildable, current regression
targets remain present, and post-batch expected titles exist exactly once.

Fresh-conversation TECH routing is still a separate acceptance test. No canonical
or private remote state is written by this module.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from candidate_batch_inspect import (
    DEFAULT_BATCH,
    DEFAULT_MASTER,
    DEFAULT_PUBLISH,
    DEFAULT_ROOT,
    DEFAULT_UPDATE,
    DEFAULT_VALIDATION,
    inspect_batch,
    join_remote,
    read_json_bytes,
    read_local,
    read_remote,
)
from library_index_build import local_record_paths, planned_indexes, remote_record_paths, validate_record

SCHEMA_VERSION = 1
PREFLIGHT_VERSION = "0.1.0"
MAX_CASES = 100


def build_virtual_catalog(
    canonical_paths: list[str],
    report: dict[str, Any],
    read_content: Callable[[str], bytes | None],
) -> tuple[list[dict[str, str]], dict[str, bytes], list[str]]:
    errors: list[str] = []
    content_by_path: dict[str, bytes] = {}

    for index, path in enumerate(sorted(set(canonical_paths))):
        raw = read_content(path)
        if raw is None:
            errors.append(f"canonical_{index}_missing:{path}")
            continue
        content_by_path[path] = raw

    for index, item in enumerate(report.get("items", [])):
        if not isinstance(item, dict):
            errors.append(f"batch_item_{index}_invalid")
            continue
        action = item.get("action")
        target = item.get("target_path")
        content_path = item.get("content_path")
        if not isinstance(target, str) or not isinstance(content_path, str):
            errors.append(f"batch_item_{index}_paths")
            continue
        if action in {"CREATE", "UPDATE"}:
            raw = read_content(content_path)
            if raw is None:
                errors.append(f"batch_item_{index}_content_missing")
                continue
            content_by_path[target] = raw
        elif action == "UNCHANGED":
            if target not in content_by_path:
                errors.append(f"batch_item_{index}_unchanged_missing")
        else:
            errors.append(f"batch_item_{index}_action")

    records: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_titles: set[tuple[str, str]] = set()
    for path, raw in sorted(content_by_path.items()):
        try:
            record = validate_record(path, raw)
        except ValueError as exc:
            errors.append(f"virtual_record_{exc}:{path}")
            continue
        record_id = record["record_id"]
        title_key = (record["record_type"], record["title"].casefold())
        if record_id in seen_ids:
            errors.append(f"virtual_duplicate_record_id:{record_id}")
        if title_key in seen_titles:
            errors.append(f"virtual_duplicate_title:{record['record_type']}:{record['title']}")
        seen_ids.add(record_id)
        seen_titles.add(title_key)
        records.append(record)

    if errors:
        return [], {}, sorted(set(errors))
    records.sort(key=lambda row: (row["domain"], row["category"], row["record_type"], row["title"].casefold()))
    return records, planned_indexes(records), []


def assess_cases(
    records: list[dict[str, str]],
    cases_payload: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(cases_payload, dict):
        return [], ["retrieval_cases_invalid"]
    cases = cases_payload.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > MAX_CASES:
        return [], ["retrieval_cases_count"]

    canonical_by_title: dict[str, list[dict[str, str]]] = {}
    for record in records:
        if record["record_type"] == "SOURCE":
            continue
        canonical_by_title.setdefault(record["title"].casefold(), []).append(record)

    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"case_{index}_invalid")
            continue
        case_id = case.get("id")
        phase = case.get("phase")
        expected = case.get("expected_titles")
        optional = case.get("optional_titles", [])
        conditional = case.get("conditional_title")
        if not isinstance(case_id, str) or phase not in {"CURRENT", "POST_CANDIDATE_BATCH"}:
            errors.append(f"case_{index}_identity")
            continue
        if not isinstance(expected, list) or any(not isinstance(title, str) for title in expected):
            errors.append(f"case_{index}_expected_titles")
            continue
        if not isinstance(optional, list) or any(not isinstance(title, str) for title in optional):
            errors.append(f"case_{index}_optional_titles")
            continue
        if conditional is not None and not isinstance(conditional, str):
            errors.append(f"case_{index}_conditional_title")
            continue

        missing: list[str] = []
        duplicates: list[str] = []
        found: list[str] = []
        for title in expected:
            matches = canonical_by_title.get(title.casefold(), [])
            if not matches:
                missing.append(title)
            elif len(matches) > 1:
                duplicates.append(title)
            else:
                found.append(matches[0]["title"])

        optional_present = [
            title for title in optional if len(canonical_by_title.get(title.casefold(), [])) == 1
        ]
        conditional_state = "NOT_APPLICABLE"
        if conditional is not None:
            matches = canonical_by_title.get(conditional.casefold(), [])
            if len(matches) == 0:
                conditional_state = "ABSENT_ALLOWED"
            elif len(matches) == 1:
                conditional_state = "PRESENT"
            else:
                conditional_state = "DUPLICATE"
                duplicates.append(conditional)

        state = "PASS" if not missing and not duplicates else "FAIL"
        results.append(
            {
                "id": case_id,
                "phase": phase,
                "state": state,
                "expected_found": found,
                "expected_missing": missing,
                "duplicate_expected_titles": sorted(set(duplicates)),
                "optional_present": optional_present,
                "conditional_title": conditional,
                "conditional_state": conditional_state,
                "routing_assertions_deferred": True,
                "forbidden_titles_not_evaluated_structurally": case.get("forbidden_titles", []),
            }
        )
    return results, sorted(set(errors))


def build_preflight_report(
    *,
    canonical_paths: list[str],
    batch_report: dict[str, Any],
    cases_payload: dict[str, Any] | None,
    read_content: Callable[[str], bytes | None],
) -> tuple[dict[str, Any] | None, list[str]]:
    records, indexes, catalog_errors = build_virtual_catalog(canonical_paths, batch_report, read_content)
    if catalog_errors:
        return None, catalog_errors
    case_results, case_errors = assess_cases(records, cases_payload)
    if case_errors:
        return None, case_errors
    failed = [row["id"] for row in case_results if row["state"] != "PASS"]
    return {
        "schema_version": SCHEMA_VERSION,
        "preflight_version": PREFLIGHT_VERSION,
        "state": "PASS" if not failed else "FAIL",
        "batch_id": batch_report.get("batch_id"),
        "virtual_record_count": len(records),
        "generated_index_count": len(indexes),
        "generated_index_paths": sorted(indexes),
        "structural_cases": case_results,
        "failed_case_ids": failed,
        "fresh_conversation_retrieval_still_required": True,
        "routing_quality_proven": False,
        "canonical_write_performed": False,
    }, []


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Candidate Retrieval Catalog Preflight",
        "",
        f"STATE: {report['state']}",
        f"BATCH_ID: {report.get('batch_id')}",
        f"VIRTUAL_RECORDS: {report['virtual_record_count']}",
        f"GENERATED_INDEXES: {report['generated_index_count']}",
        "FRESH_CONVERSATION_RETRIEVAL_STILL_REQUIRED: TRUE",
        "ROUTING_QUALITY_PROVEN: FALSE",
        "CANONICAL_WRITE_PERFORMED: FALSE",
        "",
        "This preflight proves virtual catalog/index integrity and expected-title presence only. It does not prove TECH routing behavior.",
        "",
        "## CASES",
        "",
    ]
    for case in report["structural_cases"]:
        details: list[str] = []
        if case["expected_missing"]:
            details.append("missing=" + ", ".join(case["expected_missing"]))
        if case["duplicate_expected_titles"]:
            details.append("duplicates=" + ", ".join(case["duplicate_expected_titles"]))
        if case["conditional_title"] is not None:
            details.append(
                f"conditional={case['conditional_title']}:{case['conditional_state']}"
            )
        suffix = " | " + "; ".join(details) if details else ""
        lines.append(f"- {case['id']} [{case['phase']}]: {case['state']}{suffix}")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a virtual post-batch catalog retrieval preflight.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root-dir", type=Path)
    mode.add_argument("--remote")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tests" / "retrieval_regression_cases.json",
    )
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args()

    root_base = args.root.strip("/")
    publish_rel = f"{root_base}/{DEFAULT_PUBLISH}"
    validation_rel = f"{root_base}/{DEFAULT_VALIDATION}"
    update_rel = f"{root_base}/{DEFAULT_UPDATE}"
    batch_rel = f"{root_base}/{DEFAULT_BATCH}"

    if args.root_dir is not None:
        root = args.root_dir.resolve()
        reader = lambda relative: read_local(root, relative)
        canonical_paths = local_record_paths(root)
    else:
        if not shutil.which("rclone"):
            print("candidate_retrieval_preflight_error code=rclone_missing canonical_write=0")
            return 2
        remote = str(args.remote)
        reader = lambda relative: read_remote(remote, relative)
        canonical_paths = remote_record_paths(remote)

    batch_report, batch_errors = inspect_batch(
        master_raw=reader(DEFAULT_MASTER),
        publish_raw=reader(publish_rel),
        validation_raw=reader(validation_rel),
        update_raw=reader(update_rel),
        batch_raw=reader(batch_rel),
        read_content=reader,
    )
    if batch_errors or batch_report is None:
        print(
            "candidate_retrieval_preflight_error "
            f"codes={','.join(batch_errors or ['batch_inspection_failed'])} canonical_write=0"
        )
        return 2

    try:
        cases_payload = json.loads(args.cases.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        print("candidate_retrieval_preflight_error code=retrieval_cases_read_failed canonical_write=0")
        return 2

    report, errors = build_preflight_report(
        canonical_paths=canonical_paths,
        batch_report=batch_report,
        cases_payload=cases_payload if isinstance(cases_payload, dict) else None,
        read_content=reader,
    )
    if errors or report is None:
        print(
            "candidate_retrieval_preflight_error "
            f"codes={','.join(errors or ['preflight_failed'])} canonical_write=0"
        )
        return 2

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown_out is not None:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(render_markdown(report), encoding="utf-8")

    failed = len(report["failed_case_ids"])
    print(
        "candidate_retrieval_preflight_ok "
        f"state={report['state']} batch_id={report.get('batch_id')} records={report['virtual_record_count']} "
        f"indexes={report['generated_index_count']} failed_cases={failed} routing_proven=0 canonical_write=0"
    )
    return 0 if report["state"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
