#!/usr/bin/env python3
"""Offline, proposal-only SEO sandbox. No WordPress connection or write API.

Python 3.10+, stdlib only. Reuses tech-seo-auditor.py's pure HTML checks.
TenantScope is an application boundary, not an OS sandbox against hostile code.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from os import open as open_descriptor
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlparse

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PAGES = 100
MAX_HTML_CHARS = 100_000


class SandboxError(ValueError):
    """Invalid fixture or attempted scope escape."""


def read_bounded_file(root: Path, components: tuple[str, ...], limit=MAX_FILE_BYTES) -> bytes:
    """Snapshot a regular single-link file through no-follow directory descriptors.

    The caller owns the trusted root; every relative path component is checked.
    """
    if not components or any(not isinstance(p, str) or not p or p in {".", ".."}
                             or "/" in p or "\\" in p for p in components):
        raise SandboxError("Invalid relative file path")
    if (not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
            or os.open not in os.supports_dir_fd):
        raise SandboxError("Secure reads require POSIX no-follow directory opens")
    directories = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directories.append(open_descriptor(root, flags))
        for component in components[:-1]:
            directories.append(open_descriptor(component, flags, dir_fd=directories[-1]))
        fd = open_descriptor(components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directories[-1])
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise SandboxError("Only regular files with a single hard link are accepted")
            if before.st_size > limit:
                raise SandboxError("File exceeds byte limit")
            raw = bytearray()
            while len(raw) <= limit:
                chunk = os.read(fd, min(65536, limit + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            after = os.fstat(fd)
            if len(raw) > limit:
                raise SandboxError("File exceeds byte limit")
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise SandboxError("File changed during read")
            current = os.stat(components[-1], dir_fd=directories[-1], follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
                raise SandboxError("File replaced during read")
            return bytes(raw)
        finally:
            os.close(fd)
    except OSError as exc:
        raise SandboxError("Cannot securely read file") from exc
    finally:
        for directory in reversed(directories):
            os.close(directory)


@dataclass(frozen=True)
class TenantScope:
    """An explicitly bound customer; no global active-brand fallback."""

    root: Path
    customer_id: str

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", self.customer_id):
            raise SandboxError("Invalid customer ID")
        object.__setattr__(self, "root", Path(self.root).resolve(strict=True))

    def read_bytes(self, filename: str) -> bytes:
        """Bounded, no-follow snapshot for analysis and isolation packaging."""
        if filename not in {"profile.json", "pages.json"}:
            raise SandboxError("Only fixed sandbox input files are allowed")
        return read_bounded_file(self.root, (self.customer_id, filename))

    def read(self, filename: str) -> tuple[dict, str]:
        raw = self.read_bytes(filename)
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict) or data.get("customer_id") != self.customer_id:
            raise SandboxError("Customer ownership mismatch")
        if data.get("synthetic") is not True or data.get("source") != "wordpress-fixture":
            raise SandboxError("Only explicitly synthetic WordPress fixtures are accepted")
        return data, hashlib.sha256(raw).hexdigest()


def _auditor():
    path = Path(__file__).resolve().parent / "tech-seo-auditor.py"
    spec = importlib.util.spec_from_file_location("offline_meta_auditor", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture_url(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise SandboxError("Fixture URL must be text")
    try:
        parsed = urlparse(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise SandboxError("Malformed fixture URL") from exc
    if (parsed.scheme != "https" or not host
            or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+invalid", host)
            or parsed.username is not None or parsed.password is not None
            or port is not None or ":" in parsed.netloc):
        raise SandboxError("Only https://*.invalid fixture URLs are accepted")
    return parsed.hostname


def analyze(scope: TenantScope) -> dict:
    profile, profile_hash = scope.read("profile.json")
    payload, source_hash = scope.read("pages.json")
    source_hashes = {"profile.json": profile_hash, "pages.json": source_hash}
    site_host = _fixture_url(profile.get("site_url"))
    pages = payload.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
        raise SandboxError("Pages list must contain 1 to 100 pages")
    auditor = _auditor()
    results, proposals, seen = [], [], set()
    for page in pages:
        if not isinstance(page, dict) or page.get("customer_id") != scope.customer_id:
            raise SandboxError("Page ownership mismatch")
        page_id = page.get("id")
        if type(page_id) is not int or page_id <= 0 or page_id in seen:
            raise SandboxError("Page IDs must be unique positive integers")
        seen.add(page_id)
        url = page.get("url")
        if _fixture_url(url) != site_host:
            raise SandboxError("Page belongs to another site")
        html = page.get("html")
        if not isinstance(html, str) or not html.strip() or len(html) > MAX_HTML_CHARS:
            raise SandboxError("Page HTML required")
        issues = []
        meta, deduction = auditor.check_meta(
            {"body": html.encode("utf-8"), "headers": {"content-type": "text/html"},
             "final_url": url}, url, issues)
        results.append({"page_id": page_id, "url": url, "meta": meta,
                        "issues": issues, "meta_heuristic_score": max(0, 100 + deduction)})
        suggestions = page.get("suggested_seo", {})
        if not isinstance(suggestions, dict):
            raise SandboxError("Invalid suggestions")
        for field in ("title", "description"):
            if meta[f"{field}_status"] == "good":
                continue
            proposed = suggestions.get(field)
            if proposed is not None and (not isinstance(proposed, str) or not proposed.strip()):
                raise SandboxError("Suggested SEO value must be non-empty text")
            proposals.append({"customer_id": scope.customer_id, "page_id": page_id,
                              "url": url, "field": field, "expected_before": meta[field],
                              "proposed_value": proposed,
                              "reason": meta[f"{field}_status"],
                              "source_sha256": source_hash,
                              "source_hashes": source_hashes,
                              "status": "awaiting_human_review" if proposed else "needs_authoring",
                              "human_approval_required": True, "applied": False})
    return {"customer_id": scope.customer_id, "customer_name": profile.get("name"),
            "synthetic": True, "source": "wordpress-fixture", "connection": "none",
            "mode": "proposal-only", "source_sha256": source_hash,
            "source_hashes": source_hashes,
            "checks": ["offline-html-meta"],
            "unmeasured": ["HTTP status", "redirects", "Core Web Vitals", "Google indexation"],
            "pages": results, "proposals": proposals}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Trusted sandbox customer root")
    parser.add_argument("--customer", required=True, help="Explicit customer ID")
    args = parser.parse_args()
    try:
        result = analyze(TenantScope(args.root, args.customer))
    except (SandboxError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"error": str(exc), "applied": False}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
