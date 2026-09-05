"""Publish a local file to a temporary public URL for CrewAI AMP PDF tools.

AMP runs on CrewAI servers and cannot read local disk paths. The deployed
Bank Statement PDF Extractor fetches `bank_pdf_path` when it is an http(s) URL.

Default host: tmpfiles.org (free, no API key). Override with BANK_PDF_PUBLIC_UPLOAD_URL.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_UPLOAD_URL = "https://tmpfiles.org/api/v1/upload"


class PublicUploadError(RuntimeError):
    pass


def _multipart(field: str, filename: str, data: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = "----reconqBoundary7MA4YWxkTrZu0gW"
    parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        data,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    return body, f"multipart/form-data; boundary={boundary}"


def to_direct_tmpfiles_url(page_url: str) -> str:
    """tmpfiles returns a viewer URL; AMP fetch needs the /dl/ direct link."""
    u = page_url.strip()
    if "tmpfiles.org/" in u and "/dl/" not in u:
        return u.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
    return u


def publish_public_url(path: Path | str, *, filename: str | None = None) -> str:
    """Upload file bytes and return a publicly fetchable URL."""
    p = Path(path)
    if not p.is_file():
        raise PublicUploadError(f"File not found: {p}")
    name = filename or p.name
    raw = p.read_bytes()
    if not raw:
        raise PublicUploadError("Empty file")

    endpoint = (os.getenv("BANK_PDF_PUBLIC_UPLOAD_URL") or DEFAULT_UPLOAD_URL).strip()
    body, content_type = _multipart("file", name, raw, "application/pdf")
    req = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        raise PublicUploadError(f"Public upload failed: {e}") from e

    url = None
    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        url = data.get("url") or payload.get("url") or payload.get("link")
    if not url:
        raise PublicUploadError(f"Unexpected upload response: {payload!r}")

    public = to_direct_tmpfiles_url(str(url))
    logger.info("Published %s → %s", name, public)
    return public
