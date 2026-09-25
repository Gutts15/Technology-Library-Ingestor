#!/usr/bin/env python3
"""Deterministically filter supported claims before canonical rendering.

Claim-level evidence answers whether a statement is supported. This module answers
a different question: whether that supported statement is useful and stable enough
to become canonical Technology Library material.

The gate is intentionally conservative and dependency-free. It removes obvious
repository telemetry/housekeeping facts, volatile inventories and source-layout
noise, while preserving stable functional meaning when a supported claim mixes a
volatile count with durable capability text. It then classifies remaining claims
into stable editorial buckets and enforces a type-aware minimum usefulness bar for
new TECHNOLOGY, PATTERN and PIPELINE records. Existing-record update planning
reuses the same usefulness filter without requiring the creation-time minimum again.

It never performs a canonical write and never upgrades unsupported claims.
"""

from __future__ import annotations

import re
from typing import Any

SCHEMA_VERSION = 1
EDITORIAL_GATE_VERSION = "0.3.1"

KEEP_KINDS = {
    "CAPABILITY",
    "COMPATIBILITY",
    "INSTALLATION",
    "LICENSE",
    "LIMITATION",
    "IDENTITY",
    "OTHER",
}

_REPOSITORY_METADATA = re.compile(
    r"\brepositor(?:y|ies)\b.*\b(?:has|have|exists?|owned by|is public|is private|"
    r"described as|hosted on)\b",
    re.IGNORECASE,
)
_REPOSITORY_COUNTER = re.compile(
    r"\b\d+\s+(?:stars?|forks?|open issues?|issues?|open pull requests?|pull requests?|commits?)\b",
    re.IGNORECASE,
)
_COUNTER_PHRASE = re.compile(
    r"\b(?:stars?|forks?|open issues?|open pull requests?|pull requests?|commit count|commits?)\b",
    re.IGNORECASE,
)
_VOLATILE_INVENTORY_COUNT = re.compile(
    r"\b\d+\s+(?:(?:typed|MCP)\s+)?(?:tools?|categories?|command modules?|tool handlers?|handlers?|modules?)\b",
    re.IGNORECASE,
)
_FUNCTIONAL_TOOL_COUNT = re.compile(
    r"\b(?P<verb>offers?|provides?|includes?|exposes?)\s+\d+\s+"
    r"(?P<label>(?:(?:typed|MCP)\s+)?tools?\s+(?:for|to|that)\b)",
    re.IGNORECASE,
)
_CATEGORY_LIST_COUNT = re.compile(r"\bThe\s+\d+\s+categories\s+include\b", re.IGNORECASE)
_HOUSEKEEPING_PATH = re.compile(
    r"(?:\.github|\.vscode)(?:\s+folder|\s+directory|/|\\)",
    re.IGNORECASE,
)
_INTERNAL_RUNNING_VERSION = re.compile(
    r"\bcurrent(?:ly)?\s+(?:running\s+)?version\b.*\b[0-9a-f]{6,40}\b",
    re.IGNORECASE,
)
_GENERIC_VERSION = re.compile(
    r"\b(?:version\s+(?:is\s+)?|is\s+version\s+)[vV]?[0-9][A-Za-z0-9._+-]*\b",
    re.IGNORECASE,
)
_CONTRIBUTOR_METADATA = re.compile(
    r"\b(?:submitted\s+by\s+user|community\s+submitted|part\s+of\s+the\s+.+\s+community)\b",
    re.IGNORECASE,
)
_RELEASE_DATE = re.compile(r"\breleased\s+on\s+\d{4}-\d{2}-\d{2}\b", re.IGNORECASE)
_TRIVIAL_PROJECT_LINKAGE = re.compile(
    r"\b(?:has|links?\s+to|hosted\s+on)\b.*\b(?:github\s+repository|issue\s+tracker|"
    r"(?:project|engine)\s+homepage|github)\b",
    re.IGNORECASE,
)
_DOCUMENTATION_INVENTORY = re.compile(
    r"\b(?:repository|project|package)\b.*\b(?:includes?|contains?|has)\b.*\bREADME(?:\.[A-Za-z0-9.]+)?\b",
    re.IGNORECASE,
)
_INTERNAL_SOURCE_LAYOUT = re.compile(
    r"\b(?:organized\s+under|stored\s+in|located\s+under)\b.*(?:/|\\)",
    re.IGNORECASE,
)
_FUTURE_ROADMAP = re.compile(
    r"\b(?:roadmap|future\s+(?:integration|support|feature|work)|planned\s+(?:integration|support|feature|work)|"
    r"small\s+commit\s+history|commit\s+history)\b",
    re.IGNORECASE,
)

_LICENSE_FILE_MECHANICS = re.compile(
    r"(?:\blicen[cs]e\.plist\b|\blicen[cs]e\s+file\b|(?:^|\s)/lf\b)",
    re.IGNORECASE,
)
_LICENSE = re.compile(
    r"\b(?:licen[cs]ed|licen[cs]e)\b|(?<!\.)\b(?:MIT|Apache(?:-2\.0)?|GPL|BSD)\b",
    re.IGNORECASE,
)
_DISTRIBUTION = re.compile(
    r"\b(?:available\s+(?:on|in|from)|part\s+of)\s+(?:the\s+)?(?:Godot\s+)?Asset\s+Library\b",
    re.IGNORECASE,
)
_INSTALL = re.compile(
    r"\b(?:install(?:ed|ation|ing)?|npm\s+install|pip\s+install|executable\s+is\s+located|"
    r"default\s+(?:windows|macos|linux)\s+location|located\s+in\s+the)\b",
    re.IGNORECASE,
)
_COMPAT = re.compile(
    r"\b(?:compatible\s+with|compatibility|works?\s+with|runs?\s+on|requires?\s+godot|"
    r"requires?\s+python|requires?\s+node|requires?\s+an?\s+.+license|supports?\s+godot\s+\d|"
    r"built\s+for\s+godot\s+\d)\b",
    re.IGNORECASE,
)
_LIMITATION = re.compile(
    r"\b(?:requires?|must|only\s+the|only\s+supports?|may\s+have\s+issues?|limitation|"
    r"not\s+supported|unsupported|destructive|confirm|dry_run|dry run)\b",
    re.IGNORECASE,
)
_CAPABILITY = re.compile(
    r"\b(?:supports?|provides?|offers?|allows?|enables?|connects?|bridges?|controls?|inspects?|"
    r"creates?|writes?|exports?|builds?|deploys?|tests?|runs?\s+(?:projects?|games?|tests?)|"
    r"can\s+(?:build|run|test|deploy|inspect|create|write|export|control|launch|specify|output)|"
    r"includes?\s+(?:safety|tools?|support|integration|server|addon|workflow)|"
    r"communicat(?:es?|ing)\s+with)\b",
    re.IGNORECASE,
)
_IDENTITY = re.compile(
    r"\b(?:repository|project|server|addon|plugin|package)\b.*\b(?:exists?|hosted|owned|public|community|submitted)\b",
    re.IGNORECASE,
)


def normalize_claim(text: str) -> str:
    return " ".join(text.split()).strip()


def sanitize_claim(text: str) -> str:
    """Remove volatile inventory counts when durable functional meaning survives.

    This is intentionally narrow. It does not paraphrase arbitrary claims. It only
    removes a count from two supported shapes where the remaining text is already a
    complete claim: functional tool descriptions and explicit category lists.
    Claims that are only inventory/count telemetry are left unchanged so the normal
    low-value filter can still drop them.
    """

    value = normalize_claim(text)
    value = _FUNCTIONAL_TOOL_COUNT.sub(
        lambda match: f"{match.group('verb')} {match.group('label')}", value
    )
    value = _CATEGORY_LIST_COUNT.sub("The categories include", value)
    return normalize_claim(value)


def low_value_reason(text: str) -> str | None:
    """Return a deterministic drop reason for obvious non-canonical facts."""

    value = normalize_claim(text)
    if not value:
        return "empty_claim"
    if _HOUSEKEEPING_PATH.search(value):
        return "repository_housekeeping"
    if _INTERNAL_RUNNING_VERSION.search(value):
        return "transient_runtime_version"
    if _REPOSITORY_COUNTER.search(value):
        return "repository_telemetry"
    if _COUNTER_PHRASE.search(value) and re.search(r"\b\d+\b", value):
        return "repository_telemetry"
    if _VOLATILE_INVENTORY_COUNT.search(value):
        return "volatile_inventory_count"
    if _TRIVIAL_PROJECT_LINKAGE.search(value):
        return "project_linkage_metadata"
    if _DOCUMENTATION_INVENTORY.search(value):
        return "documentation_inventory"
    if _INTERNAL_SOURCE_LAYOUT.search(value):
        return "internal_source_layout"
    if _FUTURE_ROADMAP.search(value):
        return "roadmap_or_history_noise"
    if _REPOSITORY_METADATA.search(value):
        return "repository_metadata"
    if _CONTRIBUTOR_METADATA.search(value):
        return "contributor_metadata"
    if _RELEASE_DATE.search(value):
        return "bare_release_date"
    # Bare release/package version facts age quickly. Compatibility statements
    # that happen to contain a version are preserved by the compatibility path.
    if _GENERIC_VERSION.search(value) and not _COMPAT.search(value):
        return "bare_version_fact"
    return None


def classify_claim(text: str) -> str:
    value = normalize_claim(text)
    lowered = value.casefold()

    # Repository page metadata is identity/provenance, never capability merely
    # because a quoted description contains words such as "build" or "control".
    if _REPOSITORY_METADATA.search(value):
        return "IDENTITY"
    # A path or CLI switch that points to a local license artifact is setup
    # mechanics, not evidence of the software's legal license.
    if _LICENSE_FILE_MECHANICS.search(value):
        return "INSTALLATION"
    if _LICENSE.search(value):
        return "LICENSE"
    if _DISTRIBUTION.search(value):
        return "INSTALLATION"
    if _INSTALL.search(value):
        return "INSTALLATION"
    if _COMPAT.search(value):
        return "COMPATIBILITY"
    if _LIMITATION.search(value):
        return "LIMITATION"
    if _CAPABILITY.search(value):
        return "CAPABILITY"
    if _IDENTITY.search(value):
        return "IDENTITY"
    if any(token in lowered for token in ("mcp", "model context protocol", "editor addon", "command line", "cli")):
        return "OTHER"
    return "OTHER"


def supported_claims(claim_review: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(claim_review, dict) or claim_review.get("schema_version") != 1:
        return [], ["claim_review_invalid"]
    if claim_review.get("state") != "READY":
        return [], ["claim_review_not_ready"]
    raw = claim_review.get("publication_claims")
    if not isinstance(raw, list) or not raw:
        return [], ["supported_claims_missing"]

    output: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"claim_{index}_invalid")
            continue
        claim = item.get("claim")
        support = item.get("support")
        refs = item.get("source_indices")
        if not isinstance(claim, str) or not claim.strip() or support != "SUPPORTED":
            errors.append(f"claim_{index}_not_supported")
            continue
        if not isinstance(refs, list) or not refs or any(not isinstance(value, int) for value in refs):
            errors.append(f"claim_{index}_source_refs")
            continue
        output.append(
            {
                "claim": normalize_claim(claim),
                "support": "SUPPORTED",
                "source_indices": sorted(set(refs)),
            }
        )
    return output, sorted(set(errors))


def apply_editorial_gate(
    record: dict[str, Any] | None,
    claim_review: dict[str, Any] | None,
    *,
    require_functional_evidence: bool = True,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Filter supported claims and enforce minimum canonical usefulness.

    Creation uses the default type-aware evidence minimum. Existing-record update
    planning passes `require_functional_evidence=False` so a supported compatibility,
    license or limitation correction can update an already established record
    without re-proving its full purpose.
    """

    if not isinstance(record, dict):
        return None, ["proposed_record_missing"]
    record_type = record.get("record_type")
    if record_type not in {"TECHNOLOGY", "PATTERN", "PIPELINE", "SOURCE"}:
        return None, ["record_type_invalid"]

    claims, errors = supported_claims(claim_review)
    if errors:
        return None, errors

    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    kinds: dict[str, int] = {}
    for item in claims:
        original_claim = item["claim"]
        canonical_claim = sanitize_claim(original_claim)
        reason = None if record_type == "SOURCE" else low_value_reason(canonical_claim)
        if reason:
            dropped.append({"claim": original_claim, "reason": reason})
            continue
        kind = classify_claim(canonical_claim)
        enriched = {**item, "claim": canonical_claim, "editorial_kind": kind}
        kept.append(enriched)
        kinds[kind] = kinds.get(kind, 0) + 1

    reasons: list[str] = []
    if not kept:
        reasons.append("no_canonical_claims")
    if require_functional_evidence:
        if record_type == "TECHNOLOGY" and kinds.get("CAPABILITY", 0) < 1:
            reasons.append("insufficient_functional_evidence")
        elif record_type == "PATTERN" and (kinds.get("CAPABILITY", 0) + kinds.get("OTHER", 0)) < 1:
            reasons.append("insufficient_pattern_guidance")
        elif record_type == "PIPELINE" and (kinds.get("CAPABILITY", 0) + kinds.get("OTHER", 0)) < 1:
            reasons.append("insufficient_pipeline_workflow")

    state = "READY" if not reasons else "HOLD"
    return {
        "schema_version": SCHEMA_VERSION,
        "editorial_gate_version": EDITORIAL_GATE_VERSION,
        "state": state,
        "record_type": record_type,
        "require_functional_evidence": bool(require_functional_evidence),
        "publication_claims": kept,
        "dropped_claims": dropped,
        "counts": {
            "input_supported": len(claims),
            "kept": len(kept),
            "dropped": len(dropped),
            "capability": kinds.get("CAPABILITY", 0),
            "compatibility": kinds.get("COMPATIBILITY", 0),
            "installation": kinds.get("INSTALLATION", 0),
            "license": kinds.get("LICENSE", 0),
            "limitation": kinds.get("LIMITATION", 0),
            "identity": kinds.get("IDENTITY", 0),
            "other": kinds.get("OTHER", 0),
        },
        "reasons": reasons,
        "canonical_write_performed": False,
    }, []


def claims_by_kind(review: dict[str, Any], *kinds: str) -> list[dict[str, Any]]:
    allowed = set(kinds)
    raw = review.get("publication_claims") if isinstance(review, dict) else None
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and item.get("editorial_kind") in allowed]
