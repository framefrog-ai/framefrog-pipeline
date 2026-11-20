# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Operational plumbing: signed links, job webhooks, cost accounting.

All three are opt-in and stdlib-only, so a bare FFmpeg box keeps working with
no extra dependencies. Secrets are read from the environment and never copied
into a manifest; webhook URLs are redacted before they are recorded.

Environment
-----------
``FRAMEFROG_SIGNING_SECRET``
    HMAC key for ``/jobs/{id}/download`` links. Signing is disabled when unset.
``FRAMEFROG_SIGNING_TTL``
    Default link lifetime in seconds (3600).
``FRAMEFROG_WEBHOOK_SECRET``
    HMAC key for the ``X-FrameFrog-Signature`` header.
``FRAMEFROG_GPU_HOURLY_USD`` / ``FRAMEFROG_CPU_HOURLY_USD``
    Billing rates used by :func:`compute_costs` (default 0.0 — report seconds,
    do not invent money).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from collections.abc import Iterable
from typing import Any

ENV_SIGNING_SECRET = "FRAMEFROG_SIGNING_SECRET"
ENV_SIGNING_TTL = "FRAMEFROG_SIGNING_TTL"
ENV_WEBHOOK_SECRET = "FRAMEFROG_WEBHOOK_SECRET"
ENV_GPU_RATE = "FRAMEFROG_GPU_HOURLY_USD"
ENV_CPU_RATE = "FRAMEFROG_CPU_HOURLY_USD"

DEFAULT_TTL = 3600


# --------------------------------------------------------------------------- #
# HMAC primitives
# --------------------------------------------------------------------------- #
def _secret(explicit: str | None, env_name: str) -> str:
    return explicit or os.environ.get(env_name) or ""


def _b64(digest: bytes) -> str:
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def sign_download(job_id: str, artifact: str, expires_at: int, secret: str) -> str:
    """Signature over (job, artifact, expiry) — short and URL-safe."""
    msg = f"dl:{job_id}:{artifact}:{int(expires_at)}".encode()
    return _b64(hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest())


def verify_download(job_id: str, artifact: str, expires_at: int, token: str, secret: str) -> tuple[bool, str]:
    """Return ``(ok, reason)``; constant-time on the signature compare."""
    if not secret:
        return False, "signing secret not configured"
    if int(expires_at) < time.time():
        return False, "token expired"
    expected = sign_download(job_id, artifact, int(expires_at), secret)
    if not hmac.compare_digest(expected, token or ""):
        return False, "bad signature"
    return True, "ok"


def download_url(
    base_url: str,
    job_id: str,
    artifact: str = "master",
    secret: str | None = None,
    ttl: int | None = None,
) -> str | None:
    """Build a signed download link, or ``None`` when signing is not configured."""
    secret = _secret(secret, ENV_SIGNING_SECRET)
    if not secret:
        return None
    ttl = int(ttl if ttl is not None else os.environ.get(ENV_SIGNING_TTL, DEFAULT_TTL))
    expires_at = int(time.time()) + max(1, ttl)
    token = sign_download(job_id, artifact, expires_at, secret)
    query = urllib.parse.urlencode({"expires": expires_at, "token": token})
    return f"{base_url.rstrip('/')}/jobs/{job_id}/download?{query}"


def signing_enabled() -> bool:
    return bool(_secret(None, ENV_SIGNING_SECRET))


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #
def _post_json(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator URL
        return int(resp.status)


def redact_url(url: str) -> str:
    """Drop query/fragment so tokens in the hook URL never reach the manifest."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def notify_manifest(
    manifest: dict[str, Any],
    url: str,
    secret: str | None = None,
    timeout: float = 10.0,
    attempts: int = 3,
    backoff: float = 1.5,
) -> dict[str, Any]:
    """POST the finished manifest to ``url``; never raises.

    Delivery is best-effort — the job's status must not depend on the hook.
    Every attempt and the final status are recorded so the caller can audit it.
    """
    if not url:
        return {}
    secret = _secret(secret, ENV_WEBHOOK_SECRET)
    payload = {k: v for k, v in manifest.items() if k != "manifest_path"}
    body = json.dumps(payload, default=str).encode("utf-8")
    headers = {"content-type": "application/json", "user-agent": "framefrog-pipeline"}
    if secret:
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["x-framefrog-signature"] = f"sha256={digest}"

    record: dict[str, Any] = {
        "url": redact_url(url),
        "signed": bool(secret),
        "attempts": 0,
        "delivered": False,
    }
    for attempt in range(1, max(1, attempts) + 1):
        record["attempts"] = attempt
        try:
            status = _post_json(url, body, headers, timeout)
            record["status"] = status
            if 200 <= status < 300:
                record["delivered"] = True
                record.pop("error", None)
                break
        except Exception as exc:  # network, DNS, TLS, timeouts
            record["error"] = f"{type(exc).__name__}: {exc}"
        if attempt < attempts:
            time.sleep(backoff ** (attempt - 1))
    return record


# --------------------------------------------------------------------------- #
# Cost accounting
# --------------------------------------------------------------------------- #
def _rate(name: str, explicit: float | None) -> float:
    if explicit is not None:
        return float(explicit)
    try:
        return float(os.environ.get(name, 0.0) or 0.0)
    except ValueError:
        return 0.0


def stage_class(stage: dict[str, Any]) -> str:
    """Classify a stage as ``gpu`` or ``cpu`` from the backend it actually used."""
    meta = stage.get("meta") or {}
    name = stage.get("stage")
    if name == "audio_cleanup" and meta.get("denoise_model") == "deepfilternet":
        return "gpu"
    if name == "interpolate":
        model = str(meta.get("interp_model", ""))
        if model.startswith("RIFE") or "rife" in model.lower():
            return "gpu"
    return "cpu"


def compute_costs(
    stages: Iterable[dict[str, Any]],
    gpu_hourly_usd: float | None = None,
    cpu_hourly_usd: float | None = None,
) -> dict[str, Any]:
    """Turn recorded stage durations into seconds and (optionally) dollars.

    Rates default to 0.0: the manifest always reports the measured seconds and
    the class, but never invents a price. Set the env vars (or options) to get
    real cost figures from your provider's rate card.
    """
    gpu_rate = _rate(ENV_GPU_RATE, gpu_hourly_usd)
    cpu_rate = _rate(ENV_CPU_RATE, cpu_hourly_usd)

    rows: list[dict[str, Any]] = []
    gpu_seconds = cpu_seconds = 0.0
    for stage in stages:
        seconds = float(stage.get("duration_s") or 0.0)
        cls = stage_class(stage)
        if cls == "gpu":
            gpu_seconds += seconds
            usd = seconds / 3600.0 * gpu_rate
        else:
            cpu_seconds += seconds
            usd = seconds / 3600.0 * cpu_rate
        rows.append(
            {
                "stage": stage.get("stage"),
                "status": stage.get("status"),
                "class": cls,
                "seconds": round(seconds, 3),
                "usd": round(float(usd), 6),
            }
        )

    return {
        "rates": {"gpu_hourly_usd": gpu_rate, "cpu_hourly_usd": cpu_rate},
        "gpu_seconds": round(gpu_seconds, 3),
        "cpu_seconds": round(cpu_seconds, 3),
        "stages": rows,
        "total_usd": round(sum(float(r["usd"]) for r in rows), 6),
    }


def capabilities() -> dict[str, Any]:
    """Non-secret operational snapshot for ``/health``."""
    return {
        "signing": signing_enabled(),
        "webhooks": True,
        "gpu_hourly_usd": _rate(ENV_GPU_RATE, None),
        "cpu_hourly_usd": _rate(ENV_CPU_RATE, None),
    }
