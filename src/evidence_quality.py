"""Deterministic availability gate, not a claim of semantic correctness.

Assessments contain fixed codes and channel names. Content stays in private
summaries. Unknown or weak speech cannot support automatic candidates.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

POLICY_VERSION = "0.1.0"
TEXT_CHANNELS = {
    "video": ("ocr", "speech"), "audio": ("speech",),
    "image": ("ocr_sample",), "text": ("samples",),
    "document": ("samples",), "spreadsheet": ("row_samples",),
    "link": ("samples",),
}


def has_text(channel: str, value: Any) -> bool:
    def nonempty(text: Any) -> bool:
        return isinstance(text, str) and bool(text.strip())

    if channel == "ocr_sample":
        return nonempty(value)
    if not isinstance(value, list):
        return False
    if channel == "samples":
        return any(nonempty(item) for item in value)
    if channel == "row_samples":
        return any(isinstance(row, list) and any(nonempty(cell) for cell in row) for row in value)
    if channel in {"ocr", "speech"}:
        return any(isinstance(item, dict) and nonempty(item.get("text")) for item in value)
    return False


def assess_evidence(kind: str, semantic: dict[str, Any]) -> dict[str, Any]:
    evidence = semantic.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    quality = semantic.get("quality")
    quality = quality if isinstance(quality, dict) else {}
    transcript = quality.get("transcript")
    transcript = transcript if isinstance(transcript, dict) else {}
    signal = transcript.get("signal")
    signal = signal if isinstance(signal, str) else None
    warnings = semantic.get("warnings")
    warnings = warnings if isinstance(warnings, list) else []
    allowed: list[str] = []
    blocked: list[str] = []
    reasons: list[str] = []
    for channel in TEXT_CHANNELS.get(kind, ()):
        if not has_text(channel, evidence.get(channel)):
            continue
        if channel == "speech" and (
            signal not in {"medium", "high"}
            or "transcript_low_quality_signal" in warnings
        ):
            blocked.append(channel)
            reasons.append("transcript_low_quality" if signal == "low"
                           or "transcript_low_quality_signal" in warnings
                           else "transcript_quality_unverified")
        else:
            allowed.append(channel)
    # A link is an input; the bridge still verifies fetched public content
    # before invoking a model.
    link = semantic.get("link")
    url = link.get("url_without_query_or_fragment") if isinstance(link, dict) else None
    if kind == "link" and isinstance(url, str) and url.startswith(("https://", "http://")):
        allowed.append("public_link")
    if kind not in TEXT_CHANNELS:
        reasons.append("unsupported_kind")
    elif not allowed and not reasons:
        reasons.append("no_extracted_evidence")
    return {
        "policy_version": POLICY_VERSION,
        "state": "ELIGIBLE" if allowed else "HELD",
        "allowed_channels": allowed,
        "blocked_channels": blocked,
        "reason_codes": reasons,
        "semantic_correctness_verified": False,
    }


def usable_summary(kind: str, semantic: dict[str, Any]) -> dict[str, Any]:
    assessment = assess_evidence(kind, semantic)
    result = deepcopy(semantic)
    evidence = result.get("evidence")
    if isinstance(evidence, dict):
        result["evidence"] = {
            channel: evidence[channel] for channel in assessment["allowed_channels"]
            if channel in evidence
        }
    return result


def extracted_text(kind: str, semantic: dict[str, Any]) -> str:
    """Return only usable extracted text, excluding URLs and channel metadata."""
    evidence = usable_summary(kind, semantic).get("evidence")
    if not isinstance(evidence, dict):
        return ""
    texts: list[str] = []
    for channel, value in evidence.items():
        if channel == "ocr_sample":
            values = [value]
        elif not isinstance(value, list):
            continue
        elif channel in {"ocr", "speech"}:
            values = [item.get("text") for item in value if isinstance(item, dict)]
        elif channel == "row_samples":
            values = [cell for row in value if isinstance(row, list) for cell in row]
        elif channel == "samples":
            values = value
        else:
            continue
        for text in values:
            if isinstance(text, str):
                text = re.sub(r"https?://\S+", "", text, flags=re.IGNORECASE).strip()
                if text:
                    texts.append(text)
    return "\n".join(texts)
