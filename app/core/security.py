# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Security boundaries: authentication, URI validation, path safety, SSRF.

The rules enforced here are deliberately strict, because a video pipeline
executes FFmpeg on user-supplied paths and can be pointed at object storage
or remote URLs.

* **Authentication** — an API key is required outside explicit development
  mode and compared in constant time.
* **Local paths** — must live inside ``WORK_DIR`` (or an explicitly allowed
  root). Traversal segments (``..``) are rejected outright.
* **Object storage** — only ``s3://`` is accepted, with a safe bucket/key
  split.
* **HTTP(S)** — rejected by default: a request URL is fetched by the server,
  so it is an SSRF primitive. When explicitly enabled, the resolved address
  must not be loopback, private, link-local, multicast, reserved or a cloud
  metadata endpoint.
"""

from __future__ import annotations

import hmac
import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from app.core.errors import AuthenticationError, ValidationError

#: Cloud metadata endpoints that must never be reachable.
METADATA_ADDRESSES = frozenset(
    {
        "169.254.169.254",  # AWS / GCP / Azure / DigitalOcean
        "169.254.170.2",  # AWS ECS task metadata
        "100.100.100.200",  # Alibaba Cloud
        "fd00:ec2::254",  # AWS IMDSv6
    }
)

#: Schemes the pipeline is willing to read from.
ALLOWED_URI_SCHEMES = ("s3", "file")


@dataclass(frozen=True)
class ParsedInput:
    """A validated input reference."""

    kind: str  # "s3" | "local"
    uri: str
    bucket: str | None = None
    key: str | None = None
    path: Path | None = None


def constant_time_equals(left: str, right: str) -> bool:
    """Constant-time string comparison."""
    return hmac.compare_digest((left or "").encode("utf-8"), (right or "").encode("utf-8"))


def verify_api_key(provided: str | None, expected: str | None) -> None:
    """Raise :class:`AuthenticationError` unless the key matches.

    ``expected`` empty means authentication is disabled (development only);
    the caller decides that, not this function.
    """
    if not expected:
        return
    if not provided:
        raise AuthenticationError("missing API key")
    if not constant_time_equals(provided, expected):
        raise AuthenticationError("invalid API key")


def _is_disallowed_address(address: str) -> bool:
    if address in METADATA_ADDRESSES:
        return True
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        # Not an IP literal: it was resolved to one before this point.
        return True
    return bool(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_safe_http_url(url: str) -> str:
    """Validate an outbound HTTP(S) URL against SSRF rules.

    The hostname is resolved and *every* address it maps to is checked, so a
    DNS name that points at 127.0.0.1 or 169.254.169.254 is rejected.
    """
    parts = urlparse(url)
    if parts.scheme not in {"http", "https"}:
        raise ValidationError(f"unsupported URL scheme: {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise ValidationError("URL has no host")
    if host.lower() in {"localhost", "metadata.google.internal"}:
        raise ValidationError(f"host {host!r} is not allowed")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValidationError(f"cannot resolve host {host!r}") from exc
    addresses = {str(info[4][0]) for info in infos}
    if not addresses:
        raise ValidationError(f"host {host!r} resolved to no address")
    for address in addresses:
        if _is_disallowed_address(address):
            raise ValidationError(
                f"host {host!r} resolves to a blocked address ({address}); "
                "private, loopback, link-local and cloud metadata targets are refused"
            )
    return url


def safe_relative_path(candidate: str) -> Path:
    """Normalise a relative path and refuse traversal segments."""
    if not candidate or not candidate.strip():
        raise ValidationError("empty path")
    if "\x00" in candidate:
        raise ValidationError("path contains a null byte")
    parts = Path(candidate).parts
    if any(part == ".." for part in parts):
        raise ValidationError("path traversal is not allowed")
    if Path(candidate).is_absolute():
        raise ValidationError("absolute paths are not allowed here")
    return Path(*[p for p in parts if p not in ("", ".")])


def resolve_local_input(path_like: str, allowed_roots: list[Path]) -> Path:
    """Resolve a local input path inside one of ``allowed_roots``.

    Symlinks are resolved before the containment check, so a symlink inside
    ``WORK_DIR`` pointing at ``/etc`` is refused.
    """
    if not path_like or not path_like.strip():
        raise ValidationError("input path is empty")
    if "\x00" in path_like:
        raise ValidationError("path contains a null byte")

    candidate = Path(path_like).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:  # pragma: no cover - platform dependent
        raise ValidationError(f"cannot resolve path: {exc}") from exc

    roots = [root.expanduser().resolve(strict=False) for root in allowed_roots]
    for root in roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return resolved
    raise ValidationError(f"local input must live under {', '.join(str(r) for r in roots)}")


def parse_input_uri(
    uri: str,
    *,
    allowed_roots: list[Path] | None = None,
    allow_http: bool = False,
) -> ParsedInput:
    """Validate an input URI and return its typed representation.

    Accepted forms:

    * ``s3://bucket/key`` — object storage
    * ``file:///abs/path`` or a plain path — local file inside ``allowed_roots``
    * ``http(s)://…`` — only when ``allow_http`` is true, and then only after
      the SSRF checks in :func:`assert_safe_http_url`.
    """
    if not isinstance(uri, str) or not uri.strip():
        raise ValidationError("input_uri is required")
    uri = uri.strip()
    if "\x00" in uri:
        raise ValidationError("input_uri contains a null byte")

    parts = urlparse(uri)
    scheme = parts.scheme.lower()

    if scheme == "s3":
        bucket = parts.netloc
        key = parts.path.lstrip("/")
        if not bucket:
            raise ValidationError("s3 uri is missing a bucket")
        if not key:
            raise ValidationError("s3 uri is missing an object key")
        safe_relative_path(key)
        return ParsedInput(kind="s3", uri=uri, bucket=bucket, key=key)

    if scheme == "file":
        local = parts.path or parts.netloc
        if not local:
            raise ValidationError("file uri is missing a path")
        roots = allowed_roots or [Path.cwd()]
        return ParsedInput(kind="local", uri=uri, path=resolve_local_input(local, roots))

    if scheme in {"http", "https"}:
        if not allow_http:
            raise ValidationError("http(s) inputs are disabled; upload to object storage or use a local path")
        assert_safe_http_url(uri)
        return ParsedInput(kind="http", uri=uri)

    if scheme and len(scheme) > 1:
        raise ValidationError(f"unsupported uri scheme {scheme!r}; allowed: {', '.join(ALLOWED_URI_SCHEMES)}")

    roots = allowed_roots or [Path.cwd()]
    return ParsedInput(kind="local", uri=uri, path=resolve_local_input(uri, roots))


def safe_object_key(*parts: str) -> str:
    """Build an object key from untrusted parts.

    Separators are stripped, traversal is impossible, and the result can
    never start with a slash.
    """
    cleaned: list[str] = []
    for part in parts:
        if part is None:
            continue
        text = str(part).strip().strip("/")
        if not text:
            continue
        for chunk in text.replace("\\", "/").split("/"):
            if chunk in ("", ".", ".."):
                continue
            cleaned.append(chunk)
    if not cleaned:
        raise ValidationError("cannot build an empty object key")
    return "/".join(cleaned)


def redact_credentials(text: str, secrets: dict) -> str:
    """Replace secret values inside free text (used before logging)."""
    out = text or ""
    for value in secrets.values():
        if value:
            out = out.replace(value, "***")
    return out


def environment_secret_names() -> list[str]:
    """Names of environment variables that hold secrets (never their values)."""
    return ["API_KEY", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"]
