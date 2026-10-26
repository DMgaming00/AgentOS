from __future__ import annotations

from fastapi import APIRouter

from agentos.dependencies import UserDep
from agentos.schemas.api import ToolOut
from agentos_core.tools.registry import get_registry

router = APIRouter(prefix="/api/v1/tools", tags=["tools"])


@router.get("", response_model=list[ToolOut])
async def list_tools(_: UserDep) -> list[ToolOut]:
    return [ToolOut(**m) for m in get_registry().catalog()]
