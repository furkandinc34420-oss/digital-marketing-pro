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


@dataclass(frozen=True)
class TenantScope:
    """An explicitly bound customer; no global active-brand fallback."""

    root: Path
    customer_id: str

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", self.customer_id):
            raise SandboxError("Invalid customer ID")
        object.__setattr__(self, "root", Path(self.root).resolve(strict=True))

    def read(self, filename: str) -> tuple[dict, str]:
        if filename not in {"profile.json", "pages.json"}:
            raise SandboxError("Only fixed sandbox input files are allowed")
        # Descriptor-relative opens avoid check-then-open symlink substitution.
        # Fail closed on platforms without the required POSIX primitives.
        if (not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
                or os.open not in os.supports_dir_fd):
            raise SandboxError("Secure offline reads require POSIX no-follow directory opens")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            root_fd = open_descriptor(self.root, directory_flags)
        except OSError as exc:
            raise SandboxError("Cannot securely open fixture root") from exc
        try:
            tenant_fd = open_descriptor(self.customer_id, directory_flags, dir_fd=root_fd)
            try:
                fd = open_descriptor(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=tenant_fd)
                try:
                    before = os.fstat(fd)
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                        raise SandboxError("Only regular files with a single hard link are accepted")
                    if before.st_size > MAX_FILE_BYTES:
                        raise SandboxError("Fixture exceeds file size limit")
                    raw = bytearray()
                    while len(raw) <= MAX_FILE_BYTES:
                        chunk = os.read(fd, min(65536, MAX_FILE_BYTES + 1 - len(raw)))
                        if not chunk:
                            break
                        raw.extend(chunk)
                    after = os.fstat(fd)
                    if len(raw) > MAX_FILE_BYTES:
                        raise SandboxError("Fixture exceeds file size limit")
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                            after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise SandboxError("Fixture changed during read")
                    current = os.stat(filename, dir_fd=tenant_fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
                        raise SandboxError("Fixture replaced during read")
                finally:
                    os.close(fd)
            finally:
                os.close(tenant_fd)
        except OSError as exc:
            raise SandboxError("Cannot securely read fixture file") from exc
        finally:
            os.close(root_fd)
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
