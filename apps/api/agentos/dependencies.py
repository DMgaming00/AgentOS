"""Request-scoped dependencies: DB session, authenticated user, workspace authorization."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

import jwt
from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos.security import rate_limit
from agentos.security.auth import decode_token
from agentos_core.config import get_settings
from agentos_core.db.models import User, Workspace, WorkspaceMember
from agentos_core.db.session import get_sessionmaker
from agentos_core.domain.states import Role
from agentos_core.engine.events import flush_events
from agentos_core.engine.tx import flush_notifications

bearer = HTTPBearer(auto_error=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """One transaction per request; notifications are published after commit."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await flush_events(session)
            await session.commit()
        except BaseException:
            await session.rollback()
            session.info.pop("notify", None)
            session.info.pop("pending_events", None)
            raise
        await flush_notifications(session)


# scope="function": commit before the response is sent, so a client never sees an id it cannot read yet
# (the default "request" scope runs the commit after the response).
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]


def api_error(status: int, code: str, message: str, details: Any = None) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message, "details": details})


async def current_user(
    session: SessionDep, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise api_error(401, "unauthenticated", "missing bearer token")
    try:
        claims = decode_token(creds.credentials)
        user_id = uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        raise api_error(401, "invalid_token", "token is invalid or expired") from None
    user = await session.get(User, user_id)
    if user is None:
        raise api_error(401, "invalid_token", "user no longer exists")
    return user


UserDep = Annotated[User, Depends(current_user)]

ROLE_RANK = {Role.VIEWER: 0, Role.DEVELOPER: 1, Role.ADMIN: 2}


@dataclass
class WorkspaceContext:
    user: User
    workspace_id: uuid.UUID
    role: Role

    def require(self, minimum: Role) -> None:
        if ROLE_RANK[self.role] < ROLE_RANK[minimum]:
            raise api_error(403, "forbidden", f"requires role {minimum.value} or higher in this workspace")


async def workspace_context(
    session: SessionDep,
    user: UserDep,
    x_workspace_id: Annotated[str | None, Header(alias="X-Workspace-Id")] = None,
) -> WorkspaceContext:
    memberships = (
        (
            await session.execute(
                select(WorkspaceMember)
                .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
                .where(WorkspaceMember.user_id == user.id)
                .order_by(Workspace.created_at, Workspace.id)
            )
        )
        .scalars()
        .all()
    )
    if x_workspace_id:
        try:
            wanted = uuid.UUID(x_workspace_id)
        except ValueError:
            raise api_error(400, "invalid_workspace", "X-Workspace-Id must be a UUID") from None
        member = next((m for m in memberships if m.workspace_id == wanted), None)
        if member is None:
            # Same response as a missing workspace: do not reveal existence.
            raise api_error(404, "workspace_not_found", "workspace not found")
    elif memberships:
        # Without a header the user's earliest workspace is the default (stable across new memberships).
        member = memberships[0]
    else:
        raise api_error(403, "no_workspace", "user is not a member of any workspace")
    return WorkspaceContext(user=user, workspace_id=member.workspace_id, role=Role(member.role))


WorkspaceDep = Annotated[WorkspaceContext, Depends(workspace_context)]


def rate_limited(kind: str = "default"):  # type: ignore[no-untyped-def]
    async def dep(
        request: Request, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
    ) -> None:
        s = get_settings()
        limit = s.critical_rate_limit_per_minute if kind == "critical" else s.rate_limit_per_minute
        who = creds.credentials[-24:] if creds else (request.client.host if request.client else "anon")
        allowed, retry = await rate_limit.hit(f"{kind}:{request.url.path}:{who}", limit)
        if not allowed:
            raise HTTPException(
                429,
                detail={"code": "rate_limited", "message": "too many requests"},
                headers={"Retry-After": str(retry)},
            )

    return Depends(dep)
