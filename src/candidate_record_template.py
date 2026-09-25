#!/usr/bin/env python3
"""Type-aware body templates for candidate canonical drafts/publication bytes.

Canonical record types serve different retrieval purposes. A TECHNOLOGY record
should foreground capabilities and compatibility, a PATTERN should foreground the
reusable implementation principle, a PIPELINE should foreground purpose/workflow,
and a SOURCE should preserve evidence/provenance rather than masquerade as a tool
recommendation.

This module only arranges already editorially retained claims. It does not add new
facts, does not call a model and does not write canonical storage.
"""

from __future__ import annotations

from typing import Any

from candidate_editorial_gate import claims_by_kind

TEMPLATE_VERSION = "0.2.0"
SUPPORTED_TYPES = {"TECHNOLOGY", "PATTERN", "PIPELINE", "SOURCE"}


def refs(item: dict[str, Any]) -> str:
    return ", ".join(f"S{value}" for value in item.get("source_indices", []))


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, tuple[int, ...]]] = set()
    output: list[dict[str, Any]] = []
    for item in items:
        claim = str(item.get("claim") or "").strip()
        source_indices = tuple(item.get("source_indices") or [])
        key = (claim.casefold(), source_indices)
        if not claim or key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _without(items: list[dict[str, Any]], excluded: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove summary claims from detail sections to avoid canonical repetition."""

    excluded_keys = {
        (
            str(item.get("claim") or "").strip().casefold(),
            tuple(item.get("source_indices") or []),
        )
        for item in excluded
    }
    return [
        item
        for item in items
        if (
            str(item.get("claim") or "").strip().casefold(),
            tuple(item.get("source_indices") or []),
        )
        not in excluded_keys
    ]


def summary_items(editorial: dict[str, Any], record_type: str, limit: int = 3) -> list[dict[str, Any]]:
    """Choose a compact type-aware retrieval summary without inventing prose."""

    if record_type == "TECHNOLOGY":
        groups = [
            ("CAPABILITY",),
            ("COMPATIBILITY",),
            ("INSTALLATION",),
            ("LIMITATION",),
            ("LICENSE",),
            ("IDENTITY", "OTHER"),
        ]
    elif record_type == "PATTERN":
        groups = [
            ("CAPABILITY", "OTHER"),
            ("LIMITATION",),
            ("COMPATIBILITY",),
            ("IDENTITY",),
            ("LICENSE",),
        ]
    elif record_type == "PIPELINE":
        groups = [
            ("CAPABILITY", "OTHER"),
            ("INSTALLATION",),
            ("COMPATIBILITY",),
            ("LIMITATION",),
            ("LICENSE",),
        ]
    else:  # SOURCE
        groups = [
            ("CAPABILITY", "OTHER", "IDENTITY"),
            ("COMPATIBILITY", "INSTALLATION"),
            ("LIMITATION",),
            ("LICENSE",),
        ]

    ordered: list[dict[str, Any]] = []
    for kinds in groups:
        for item in claims_by_kind(editorial, *kinds):
            if item not in ordered:
                ordered.append(item)
            if len(ordered) >= limit:
                return ordered
    return ordered


def section_plan(record_type: str, editorial: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Map retained claim kinds to retrieval-oriented sections for each type."""

    if record_type not in SUPPORTED_TYPES:
        raise ValueError("record_type_invalid")

    capability = claims_by_kind(editorial, "CAPABILITY")
    compatibility = claims_by_kind(editorial, "COMPATIBILITY")
    installation = claims_by_kind(editorial, "INSTALLATION")
    limitation = claims_by_kind(editorial, "LIMITATION")
    license_items = claims_by_kind(editorial, "LICENSE")
    identity = claims_by_kind(editorial, "IDENTITY")
    other = claims_by_kind(editorial, "OTHER")

    if record_type == "TECHNOLOGY":
        return [
            ("VERIFIED CAPABILITIES", capability),
            ("COMPATIBILITY AND SETUP", _dedupe(compatibility + installation)),
            ("LIMITATIONS AND SAFETY", limitation),
            ("LICENSE", license_items),
            ("OTHER VERIFIED TECHNICAL NOTES", _dedupe(identity + other)),
        ]

    if record_type == "PATTERN":
        return [
            ("IMPLEMENTATION GUIDANCE", _dedupe(capability + installation + other)),
            ("CONSTRAINTS AND LIMITATIONS", limitation),
            ("COMPATIBILITY", compatibility),
            ("PROVENANCE NOTES", identity),
            ("LICENSE", license_items),
        ]

    if record_type == "PIPELINE":
        return [
            ("VERIFIED WORKFLOW", _dedupe(capability + other)),
            ("SETUP AND PREREQUISITES", _dedupe(installation + compatibility)),
            ("LIMITATIONS AND SAFETY", limitation),
            ("PROVENANCE NOTES", identity),
            ("LICENSE", license_items),
        ]

    return [
        ("SOURCE EVIDENCE", _dedupe(capability + other + identity)),
        ("VERIFICATION CONTEXT", _dedupe(compatibility + installation + limitation + license_items)),
    ]


def render_body(record_type: str, title: str, editorial: dict[str, Any]) -> list[str]:
    """Render body lines up to, but not including, references/validation notes."""

    if record_type not in SUPPORTED_TYPES:
        raise ValueError("record_type_invalid")

    intro_heading = {
        "TECHNOLOGY": "SUMMARY",
        "PATTERN": "SUMMARY",
        "PIPELINE": "PURPOSE",
        "SOURCE": "SOURCE CONTENT",
    }[record_type]

    summary = summary_items(editorial, record_type)
    lines = [f"# {title}", "", f"## {intro_heading}", ""]
    for item in summary:
        lines.append(f"- {item['claim']} [{refs(item)}]")

    for heading, items in section_plan(record_type, editorial):
        remaining = _without(items, summary)
        if not remaining:
            continue
        lines.extend(["", f"## {heading}", ""])
        for item in remaining:
            lines.append(f"- {item['claim']} [{refs(item)}]")
    return lines
