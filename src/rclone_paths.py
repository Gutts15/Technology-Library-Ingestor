"""Path helpers for rclone remotes."""

from __future__ import annotations


def join_remote(remote: str, relative: str) -> str:
    """Join paths, keeping a bare ``name:`` remote distinct from ``name:/``."""
    root = remote.rstrip("/")
    rel = relative.strip("/")
    if not rel:
        return root
    separator = "" if root.endswith(":") else "/"
    return f"{root}{separator}{rel}"
