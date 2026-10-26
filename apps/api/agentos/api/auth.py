from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from agentos.dependencies import SessionDep, UserDep, api_error, rate_limited
from agentos.schemas.api import LoginRequest, Membership, MeResponse, TokenResponse, UserOut
from agentos.security.auth import create_token
from agentos_core.config import get_settings
from agentos_core.db.models import User, Workspace, WorkspaceMember
from agentos_core.security_passwords import verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


async def memberships(session: SessionDep, user: User) -> list[Membership]:
    rows = (
        await session.execute(
            select(WorkspaceMember.workspace_id, Workspace.name, WorkspaceMember.role)
            .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
            .where(WorkspaceMember.user_id == user.id)
            .order_by(Workspace.created_at, Workspace.id)
        )
    ).all()
    return [Membership(workspace_id=w, workspace_name=n, role=r) for w, n, r in rows]


@router.post("/login", response_model=TokenResponse, dependencies=[rate_limited("critical")])
async def login(body: LoginRequest, session: SessionDep) -> TokenResponse:
    if not get_settings().dev_auth_enabled:
        raise api_error(403, "dev_auth_disabled", "development login is disabled")
    user = (
        await session.execute(select(User).where(User.email == body.email.lower().strip()))
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise api_error(401, "invalid_credentials", "invalid email or password")
    token, ttl = create_token(user.id, user.email)
    return TokenResponse(
        access_token=token,
        expires_in=ttl,
        user=UserOut.model_validate(user),
        memberships=await memberships(session, user),
    )


@router.get("/me", response_model=MeResponse)
async def me(user: UserDep, session: SessionDep) -> MeResponse:
    return MeResponse(user=UserOut.model_validate(user), memberships=await memberships(session, user))
