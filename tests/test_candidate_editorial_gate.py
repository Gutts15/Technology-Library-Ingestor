#!/usr/bin/env python3
"""Dependency-free smoke tests for canonical editorial filtering."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_editorial_gate import (
    EDITORIAL_GATE_VERSION,
    apply_editorial_gate,
    classify_claim,
    low_value_reason,
    sanitize_claim,
)


def review_for(claims: list[str]) -> dict:
    return {
        "schema_version": 1,
        "state": "READY",
        "publication_claims": [
            {"claim": claim, "support": "SUPPORTED", "source_indices": [1]}
            for claim in claims
        ],
    }


def main() -> None:
    assert low_value_reason("The repository has 13 stars.") == "repository_telemetry"
    assert low_value_reason("The repository has 6 forks.") == "repository_telemetry"
    assert low_value_reason("The repository contains a .github folder.") == "repository_housekeeping"
    assert low_value_reason("The addon is currently running version 11b303d.") == "transient_runtime_version"
    assert low_value_reason("Godot Editor MCP is version 2026.08.31b3.") == "bare_version_fact"
    assert low_value_reason("Sample Editor Bridge was submitted by user sample-contributor.") == "contributor_metadata"
    assert low_value_reason("Godot Editor MCP was community submitted.") == "contributor_metadata"
    assert low_value_reason("Sample Editor Bridge is part of the sample engine community.") == "contributor_metadata"
    assert low_value_reason("Sample Editor Bridge was released on 2026-08-13.") == "bare_release_date"
    assert low_value_reason("It offers 180 typed tools for editor control.") == "volatile_inventory_count"
    assert low_value_reason("The system includes 173 MCP tools distributed across 26 categories.") == "volatile_inventory_count"
    assert low_value_reason("The command router aggregates 24 command modules.") == "volatile_inventory_count"
    assert low_value_reason("The Sample Editor Bridge has a GitHub repository.") == "project_linkage_metadata"
    assert low_value_reason("The Sample Editor Bridge has an issue tracker.") == "project_linkage_metadata"
    assert low_value_reason("The repository includes README.md and README.zh.md documentation files.") == "documentation_inventory"
    assert low_value_reason("The plugin command modules are organized under addons/godot_mcp/commands/.") == "internal_source_layout"
    assert low_value_reason("The project roadmap mentions future Igor integration.") == "roadmap_or_history_noise"
    assert low_value_reason("The addon runs on Godot 4.4.") is None

    assert (
        sanitize_claim("It offers 180 typed tools for inspecting, editing, scripting, running, and exporting games.")
        == "It offers typed tools for inspecting, editing, scripting, running, and exporting games."
    )
    assert (
        sanitize_claim("The 26 categories include Project, Scene, Node, Script, Runtime, and Testing/QA.")
        == "The categories include Project, Scene, Node, Script, Runtime, and Testing/QA."
    )
    assert (
        sanitize_claim("The system includes 173 MCP tools distributed across 26 categories.")
        == "The system includes 173 MCP tools distributed across 26 categories."
    )
    assert low_value_reason(
        sanitize_claim("It offers 180 typed tools for inspecting, editing, scripting, running, and exporting games.")
    ) is None

    assert classify_claim("The repository is described as 'Build GM projects with AI'.") == "IDENTITY"
    assert classify_claim("The server allows AI assistants to control the Godot editor.") == "CAPABILITY"
    assert classify_claim("The project is licensed under MIT.") == "LICENSE"
    assert classify_claim("GPL applies to this project.") == "LICENSE"
    assert classify_claim("PaletteForge can import .gpl, .hex, and .pal palette files.") != "LICENSE"
    assert classify_claim("The addon runs on Godot 4.4.") == "COMPATIBILITY"
    assert classify_claim("The addon is built for Godot 4.4.") == "COMPATIBILITY"
    assert classify_claim("The package can be installed with npm install.") == "INSTALLATION"
    assert classify_claim("The addon is available on the Godot Asset Library.") == "INSTALLATION"
    assert classify_claim("The /lf option specifies the full path to a licence.plist file.") == "INSTALLATION"

    technology = {"record_type": "TECHNOLOGY"}
    useful_review = review_for(
        [
            "The repository has 13 stars.",
            "Sample Editor Bridge was submitted by user sample-contributor.",
            "Sample Editor Bridge was released on 2026-08-13.",
            "The server allows AI assistants to control the Godot 4 editor.",
            "The project is licensed under MIT.",
            "The addon runs on Godot 4.4.",
        ]
    )
    gated, errors = apply_editorial_gate(technology, useful_review)
    assert not errors, errors
    assert gated is not None
    assert gated["state"] == "READY"
    assert gated["editorial_gate_version"] == EDITORIAL_GATE_VERSION
    assert gated["counts"]["input_supported"] == 6
    assert gated["counts"]["kept"] == 3
    assert gated["counts"]["dropped"] == 3
    assert gated["counts"]["capability"] == 1
    assert gated["canonical_write_performed"] is False
    assert all("stars" not in item["claim"].casefold() for item in gated["publication_claims"])
    assert all("submitted" not in item["claim"].casefold() for item in gated["publication_claims"])

    noise_review = review_for(
        [
            "The system includes 173 MCP tools distributed across 26 categories.",
            "The Sample Editor Bridge has a GitHub repository.",
            "The repository includes README.md and README.zh.md documentation files.",
            "The project roadmap mentions future Igor integration.",
            "The server allows AI assistants to control the Godot editor.",
        ]
    )
    noise_gated, noise_errors = apply_editorial_gate(technology, noise_review)
    assert not noise_errors
    assert noise_gated is not None and noise_gated["state"] == "READY"
    assert noise_gated["counts"]["kept"] == 1
    assert noise_gated["counts"]["dropped"] == 4
    assert noise_gated["counts"]["capability"] == 1

    mixed_inventory = review_for(
        [
            "It offers 180 typed tools for inspecting, editing, scripting, running, and exporting games.",
            "The 26 categories include Project, Scene, Node, Script, Runtime, and Testing/QA.",
            "The system includes 173 MCP tools distributed across 26 categories.",
        ]
    )
    mixed_gated, mixed_errors = apply_editorial_gate(technology, mixed_inventory)
    assert not mixed_errors
    assert mixed_gated is not None and mixed_gated["state"] == "READY"
    assert mixed_gated["counts"]["kept"] == 2
    assert mixed_gated["counts"]["dropped"] == 1
    assert mixed_gated["counts"]["capability"] == 1
    mixed_claims = [item["claim"] for item in mixed_gated["publication_claims"]]
    assert "It offers typed tools for inspecting, editing, scripting, running, and exporting games." in mixed_claims
    assert "The categories include Project, Scene, Node, Script, Runtime, and Testing/QA." in mixed_claims

    license_mechanics = review_for(
        [
            "GameMaker provides a command-line interface that can build projects.",
            "The /lf option specifies the full path to a licence.plist file.",
        ]
    )
    license_gated, license_errors = apply_editorial_gate(technology, license_mechanics)
    assert not license_errors
    assert license_gated is not None and license_gated["state"] == "READY"
    assert license_gated["counts"]["license"] == 0
    assert license_gated["counts"]["installation"] == 1

    metadata_only = review_for(
        [
            "The repository example-org/sample-mcp-server exists on GitHub.",
            "The repository example-org/sample-mcp-server is public.",
            "The repository example-org/sample-mcp-server is described as 'Sample editor server for agents'.",
            "The repository example-org/sample-mcp-server has 11 stars.",
        ]
    )
    held, held_errors = apply_editorial_gate(technology, metadata_only)
    assert not held_errors, held_errors
    assert held is not None
    assert held["state"] == "HOLD"
    assert "insufficient_functional_evidence" in held["reasons"]
    assert held["counts"]["capability"] == 0

    pattern_meta = review_for(
        [
            "The pattern is compatible with GameMaker and Godot.",
            "The pattern itself has no software license.",
        ]
    )
    held_pattern, pattern_errors = apply_editorial_gate({"record_type": "PATTERN"}, pattern_meta)
    assert not pattern_errors
    assert held_pattern is not None and held_pattern["state"] == "HOLD"
    assert "insufficient_pattern_guidance" in held_pattern["reasons"]

    pattern_guidance = review_for(
        [
            "Keep apparent volume roughly constant while deforming the sprite.",
            "The pattern is compatible with GameMaker and Godot.",
        ]
    )
    ready_pattern, pattern_errors = apply_editorial_gate({"record_type": "PATTERN"}, pattern_guidance)
    assert not pattern_errors
    assert ready_pattern is not None and ready_pattern["state"] == "READY"
    assert ready_pattern["counts"]["other"] >= 1

    pipeline_meta = review_for(
        [
            "The workflow requires Node.js 22.",
            "The workflow is licensed under MIT.",
        ]
    )
    held_pipeline, pipeline_errors = apply_editorial_gate({"record_type": "PIPELINE"}, pipeline_meta)
    assert not pipeline_errors
    assert held_pipeline is not None and held_pipeline["state"] == "HOLD"
    assert "insufficient_pipeline_workflow" in held_pipeline["reasons"]

    pipeline_guidance = review_for(
        [
            "Validate the specification before implementation begins.",
            "The workflow requires Node.js 22.",
        ]
    )
    ready_pipeline, pipeline_errors = apply_editorial_gate({"record_type": "PIPELINE"}, pipeline_guidance)
    assert not pipeline_errors
    assert ready_pipeline is not None and ready_pipeline["state"] == "READY"

    update_only, update_errors = apply_editorial_gate(
        {"record_type": "PIPELINE"},
        pipeline_meta,
        require_functional_evidence=False,
    )
    assert not update_errors
    assert update_only is not None and update_only["state"] == "READY"

    source_record = {"record_type": "SOURCE"}
    source_gate, source_errors = apply_editorial_gate(source_record, metadata_only)
    assert not source_errors, source_errors
    assert source_gate is not None
    assert source_gate["state"] == "READY"

    print(
        "candidate_editorial_gate_smoke_ok "
        "volatile_filtered=1 inventory_filtered=1 mixed_claim_sanitized=1 project_noise_filtered=1 "
        "license_semantics=1 contributor_noise_filtered=1 technology_minimum=1 "
        "pattern_minimum=1 pipeline_minimum=1 update_exception=1 source_bypass=1 "
        f"gate_version={EDITORIAL_GATE_VERSION} canonical_write=0"
    )


if __name__ == "__main__":
    main()
