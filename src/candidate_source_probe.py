#!/usr/bin/env python3
"""Fetch bounded evidence only from URLs explicitly present in one candidate.

This is not a general web-search engine. It is a zero-additional-cost evidence
probe used before local semantic validation. Candidate source text is treated as
untrusted data, redirects are checked, private/local network targets are blocked,
and stdout never prints URLs or page contents.

For an explicit public github.com owner/repository URL, the probe first attempts a
bounded README fetch through GitHub's public REST endpoint. That derived request is
strictly limited to the same repository identity and falls back to the original
repository page when README retrieval is unavailable.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
PROBE_VERSION = "0.2.0"
MAX_SOURCES = 8
MAX_RESPONSE_BYTES = 256 * 1024
MAX_EXCERPT_CHARS = 12000
URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.IGNORECASE)
GITHUB_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip == 0 and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return " ".join(self.parts)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_url(raw: str) -> str | None:
    value = raw.rstrip(".,;:!?)\"]}'")
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    if parsed.port not in {None, 80, 443}:
        return None
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, "")
    )


def extract_urls(text: str, limit: int = MAX_SOURCES) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.findall(text):
        normalized = normalize_url(match)
        if normalized and normalized not in seen:
            output.append(normalized)
            seen.add(normalized)
        if len(output) >= limit:
            break
    return output


def address_is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def hostname_is_public(hostname: str) -> bool:
    if hostname.lower() in {"localhost", "localhost.localdomain"}:
        return False
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addresses = {item[4][0] for item in infos if item and item[4]}
    return bool(addresses) and all(address_is_public(address) for address in addresses)


def url_is_public(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return bool(parsed.hostname) and hostname_is_public(parsed.hostname)


def github_repo_coordinates(url: str) -> tuple[str, str] | None:
    """Return owner/repo only for a direct explicit github.com repository URL."""

    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if (parsed.hostname or "").lower() not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        return None
    owner, repo = parts
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        return None
    if not GITHUB_COMPONENT_RE.fullmatch(owner) or not GITHUB_COMPONENT_RE.fullmatch(repo):
        return None
    return owner, repo


def github_readme_api_url(url: str) -> str | None:
    coordinates = github_repo_coordinates(url)
    if coordinates is None:
        return None
    owner, repo = coordinates
    return "https://api.github.com/repos/{}/{}/readme".format(
        urllib.parse.quote(owner, safe=""), urllib.parse.quote(repo, safe="")
    )


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        normalized = normalize_url(newurl)
        if not normalized or not url_is_public(normalized):
            raise urllib.error.HTTPError(newurl, code, "unsafe_redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, normalized)


def compact_text(raw: bytes, content_type: str) -> str:
    charset = "utf-8"
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type, re.IGNORECASE)
    if match:
        charset = match.group(1)
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")

    if "html" in content_type.lower() or "<html" in text[:1000].lower():
        parser = TextExtractor()
        try:
            parser.feed(text)
            text = parser.text()
        except Exception:
            pass
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_EXCERPT_CHARS]


def _read_bounded(response) -> tuple[bytes, bool]:
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    truncated = len(raw) > MAX_RESPONSE_BYTES
    return raw[:MAX_RESPONSE_BYTES], truncated


def _fetch_public_url(url: str, timeout: float) -> dict[str, Any]:
    if not url_is_public(url):
        return {"status": "BLOCKED", "reason": "non_public_target"}

    opener = urllib.request.build_opener(SafeRedirectHandler())
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Technology-Library-Evidence-Probe/0.2",
            "Accept": "text/html,text/plain,application/json;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "identity",
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw, truncated = _read_bounded(response)
            content_type = response.headers.get("Content-Type", "")
            excerpt = compact_text(raw, content_type)
            final_url = normalize_url(response.geturl()) or url
            return {
                "status": "OK",
                "http_status": int(getattr(response, "status", 200)),
                "final_url": final_url,
                "content_type": content_type[:160],
                "bytes_read": len(raw),
                "truncated": truncated,
                "content_sha256": hashlib.sha256(raw).hexdigest(),
                "excerpt": excerpt,
                "evidence_kind": "source_page",
            }
    except urllib.error.HTTPError as exc:
        return {"status": "ERROR", "reason": "http_error", "http_status": int(exc.code)}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"status": "ERROR", "reason": "network_error"}


def _github_readme_bytes(raw: bytes, content_type: str) -> bytes | None:
    # GitHub's raw media type may still include "+json" in Content-Type while
    # returning raw README bytes. Inspect the payload shape before treating it as
    # a JSON metadata response.
    if not raw.lstrip().startswith(b"{"):
        return raw
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    content = payload.get("content")
    encoding = payload.get("encoding")
    if isinstance(content, str) and encoding == "base64":
        try:
            return base64.b64decode(content, validate=False)
        except (ValueError, TypeError):
            return None
    return None


def fetch_github_readme(url: str, timeout: float) -> dict[str, Any]:
    api_url = github_readme_api_url(url)
    if api_url is None:
        return {"status": "ERROR", "reason": "not_direct_github_repository"}
    if not url_is_public(api_url):
        return {"status": "BLOCKED", "reason": "non_public_github_api"}

    opener = urllib.request.build_opener(SafeRedirectHandler())
    request = urllib.request.Request(
        api_url,
        headers={
            "User-Agent": "Technology-Library-Evidence-Probe/0.2",
            "Accept": "application/vnd.github.raw+json, application/json;q=0.9",
            "Accept-Encoding": "identity",
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw, truncated = _read_bounded(response)
            content_type = response.headers.get("Content-Type", "")
            readme = _github_readme_bytes(raw, content_type)
            if readme is None:
                return {"status": "ERROR", "reason": "github_readme_decode_failed"}
            readme = readme[:MAX_RESPONSE_BYTES]
            excerpt = compact_text(readme, "text/plain; charset=utf-8")
            if not excerpt:
                return {"status": "ERROR", "reason": "github_readme_empty"}
            return {
                "status": "OK",
                "http_status": int(getattr(response, "status", 200)),
                "final_url": normalize_url(response.geturl()) or api_url,
                "content_type": "text/markdown",
                "bytes_read": len(readme),
                "truncated": truncated,
                "content_sha256": hashlib.sha256(readme).hexdigest(),
                "excerpt": excerpt,
                "evidence_kind": "github_readme",
                "derived_request": True,
            }
    except urllib.error.HTTPError as exc:
        return {"status": "ERROR", "reason": "github_readme_http_error", "http_status": int(exc.code)}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"status": "ERROR", "reason": "github_readme_network_error"}


def fetch_one(url: str, timeout: float) -> dict[str, Any]:
    if github_repo_coordinates(url) is not None:
        readme = fetch_github_readme(url, timeout)
        if readme.get("status") == "OK":
            return readme
        fallback = _fetch_public_url(url, timeout)
        if fallback.get("status") == "OK":
            fallback["evidence_kind"] = "github_repository_page_fallback"
            fallback["readme_fallback_reason"] = str(readme.get("reason") or "readme_unavailable")
        return fallback
    return _fetch_public_url(url, timeout)


def build_probe(candidate_text: str, *, timeout: float = 8.0, max_sources: int = MAX_SOURCES) -> dict[str, Any]:
    urls = extract_urls(candidate_text, max_sources)
    results: list[dict[str, Any]] = []
    for url in urls:
        item = fetch_one(url, timeout)
        item["source_url"] = url
        item["source_url_sha256"] = hashlib.sha256(url.encode("utf-8")).hexdigest()
        results.append(item)

    return {
        "schema_version": SCHEMA_VERSION,
        "probe_version": PROBE_VERSION,
        "generated_at": utc_now(),
        "source_count": len(results),
        "successful_sources": sum(1 for item in results if item.get("status") == "OK"),
        "blocked_sources": sum(1 for item in results if item.get("status") == "BLOCKED"),
        "failed_sources": sum(1 for item in results if item.get("status") == "ERROR"),
        "sources": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe explicit candidate source URLs safely and cheaply.")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--max-sources", type=int, default=MAX_SOURCES)
    args = parser.parse_args()

    try:
        candidate_text = args.candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        print("candidate_source_probe_error code=candidate_read_failed")
        return 2

    probe = build_probe(
        candidate_text,
        timeout=max(1.0, min(float(args.timeout), 20.0)),
        max_sources=max(1, min(int(args.max_sources), MAX_SOURCES)),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(probe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_source_probe_ok "
        f"sources={probe['source_count']} ok={probe['successful_sources']} "
        f"blocked={probe['blocked_sources']} failed={probe['failed_sources']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
