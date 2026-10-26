"""Sanitized, verifiable export of a flight recording (attach to a bug report).

The export is *fail-closed*: after redaction it is scanned again for secret patterns and credential-like
keys, and the export is refused rather than emitted if anything remains.
"""

from __future__ import annotations

import json
from typing import Any

from agentos_core.flight.timeline import canonical_hash
from agentos_core.redaction import SECRET_PATTERNS, SENSITIVE_KEYS, redact
from agentos_core.util import utcnow

DROP_KEYS = {"lease_token", "lease_owner", "password_hash", "authorization", "cookie"}


class ExportRefusedError(RuntimeError):
    """Sanitization could not guarantee the package is free of credentials."""


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in DROP_KEYS}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _find_secrets(value: Any, path: str = "$") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if (
                isinstance(k, str)
                and SENSITIVE_KEYS.search(k)
                and not k.endswith("_tokens")
                and v not in ("[REDACTED]", None)
            ):
                hits.append(f"{path}.{k}")
            hits += _find_secrets(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            hits += _find_secrets(v, f"{path}[{i}]")
    elif isinstance(value, str):
        hits += [path for p in SECRET_PATTERNS if p.search(value)]
    return hits


def build_export(recording: dict[str, Any], model_call_bodies: dict[str, dict[str, Any]]) -> dict[str, Any]:
    package: dict[str, Any] = {
        "format": "agentos.flight-export",
        "schema_version": recording["schema_version"],
        "exported_at": utcnow().isoformat(),
        "sanitized": True,
        "notes": [
            "Secrets are redacted; provider-raw payloads and lease tokens are not included.",
            "Costs are estimates (simulated pricing for mock, fixture and recorded providers).",
            "Replay this package with the recorded-replay view; it is data, not executable.",
        ],
        "recording": recording,
        "model_call_bodies": model_call_bodies,
    }
    package = redact(_scrub(package))
    leaks = _find_secrets(package)
    if leaks:
        raise ExportRefusedError(f"export refused: possible credentials remain at {leaks[:5]}")
    package["integrity"] = {
        "frames_sha256": recording["integrity"]["sha256"],
        "package_sha256": canonical_hash({k: v for k, v in package.items() if k != "integrity"}),
    }
    return package


def to_json(package: dict[str, Any]) -> str:
    return json.dumps(package, indent=2, sort_keys=True, default=str)
