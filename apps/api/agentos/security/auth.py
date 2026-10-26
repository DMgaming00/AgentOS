"""Development authentication: email + password -> signed JWT (HS256).

This is a documented *development* flow (seeded users, shared secret). It is
real authentication - every resource endpoint requires a valid token and a
workspace membership - but it is not an identity provider. Production refuses to
start with the default secret or with dev auth enabled (``Settings.ensure_safe``).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import jwt

from agentos_core.config import get_settings
from agentos_core.util import utcnow

ALGORITHM = "HS256"
ISSUER = "agentos-dev"


def create_token(user_id: uuid.UUID, email: str) -> tuple[str, int]:
    s = get_settings()
    now = utcnow()
    payload = {
        "sub": str(user_id),
        "email": email,
        "iss": ISSUER,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=s.jwt_ttl_seconds)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, s.jwt_secret.get_secret_value(), algorithm=ALGORITHM), s.jwt_ttl_seconds


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        get_settings().jwt_secret.get_secret_value(),
        algorithms=[ALGORITHM],
        issuer=ISSUER,
        options={"require": ["exp", "sub", "iss"]},
    )
