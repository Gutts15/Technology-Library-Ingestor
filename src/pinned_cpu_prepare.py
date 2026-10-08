"""Bounded startup self-test with empty synthetic evidence, never private input."""

from file_evidence_candidate_bridge import AUTOMATIC_MODEL_RESPONSE_SCHEMA, build_automatic_prompt
from pinned_cpu_inference import infer


def prepare(timeout: float = 90, *, endpoint: str = "http://127.0.0.1:11434") -> bool:
    prompt = build_automatic_prompt(
        {"kind": "document", "semantic_summary": {"evidence": {"samples": []}}},
        {"url": "https://example.invalid/model-readiness", "excerpt": ""},
    )
    response = infer(prompt, AUTOMATIC_MODEL_RESPONSE_SCHEMA, timeout, endpoint=endpoint)
    return bool(isinstance(response, dict)
                and response.get("outcome") in {"NO_REUSABLE_KNOWLEDGE", "NEEDS_REVIEW"}
                and response.get("candidates") == [])
