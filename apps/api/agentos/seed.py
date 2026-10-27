"""Seed development users, the demo workspace, agents, sample documents and workflows.

Usage: ``python -m agentos.seed`` (idempotent; safe to run repeatedly).
"""

from __future__ import annotations

import asyncio

from agentos_core.config import get_settings
from agentos_core.db.session import dispose_engine
from agentos_core.demo import DEV_USERS, seed
from agentos_core.engine.tx import unit_of_work


async def main() -> None:
    s = get_settings()
    if s.environment == "production":
        raise SystemExit("refusing to seed development users in production")
    async with unit_of_work() as session:
        ws = await seed(session)
        from agentos_core.arena.contenders import seed_arena

        await seed_arena(session, ws, admin_id=None)
    await dispose_engine()
    print(f"seeded workspace {ws.name} ({ws.id})")
    for email, role, password in DEV_USERS:
        print(f"  dev login: {email} / {password}  [{role}]")


if __name__ == "__main__":
    asyncio.run(main())
