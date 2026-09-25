#!/usr/bin/env python3
"""Smoke tests for deterministic GitHub repository README routing."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_source_probe import github_readme_api_url, github_repo_coordinates


def main() -> None:
    assert github_repo_coordinates("https://github.com/mkdevkit/godot-mcp") == (
        "mkdevkit",
        "godot-mcp",
    )
    assert github_repo_coordinates("https://github.com/mkdevkit/godot-mcp.git") == (
        "mkdevkit",
        "godot-mcp",
    )
    assert github_repo_coordinates("https://www.github.com/example-org/sample-mcp-server/") == (
        "example-org",
        "sample-mcp-server",
    )

    assert github_repo_coordinates("https://github.com/mkdevkit/godot-mcp/issues") is None
    assert github_repo_coordinates("https://github.com/mkdevkit/godot-mcp/blob/main/README.md") is None
    assert github_repo_coordinates("https://example.com/mkdevkit/godot-mcp") is None
    assert github_repo_coordinates("https://github.com/a/b/c") is None

    assert github_readme_api_url("https://github.com/mkdevkit/godot-mcp") == (
        "https://api.github.com/repos/mkdevkit/godot-mcp/readme"
    )
    assert github_readme_api_url("https://github.com/mkdevkit/godot-mcp/issues") is None

    print(
        "candidate_source_probe_github_smoke_ok "
        "direct_repo_only=1 bounded_readme_route=1 network_call=0"
    )


if __name__ == "__main__":
    main()
