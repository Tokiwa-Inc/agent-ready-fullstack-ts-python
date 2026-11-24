"""A snapshot on Postgres: one transaction, READ ONLY and REPEATABLE READ, that sees no write
committed after it opened, except one the request made through `scoped`.
`docs/adr/template/0033`."""

import asyncio
from dataclasses import replace
from typing import Any, Final

import asyncpg
import httpx
import pytest
from fastapi import APIRouter

from app.audit_events import acting_as
from app.deployment import READS_PART_BOUND_SECONDS_ENV
from app.jobs import kinds
from app.models import CreateTaskBody
from app.reads import PART
from app.routes import public
from app.store import Database
from app.store.acting import SET_TENANT_AND_ACTING, acting
from app.store.pg import PostgresDatabase
from app.store.snapshots import snapshotted
from app.store.timeouts import Timeouts
from tests.event_loop import Reported
from tests.integration.conftest import ONE_PROCESS, THE_SUITE, Provisioned
from tests.routed import assembled

TENANT: Final = "acme"

COUNT: Final = "SELECT count(*) FROM tasks"

COUNT_AGAIN: Final = "SELECT count(*) FROM tasks WHERE true"

INSERT: Final = "INSERT INTO tasks (id, tenant_id, title) VALUES (gen_random_uuid(), $1, $2)"

PART_BOUND_SECONDS: Final = 0.2

GROUPS: Final = 5

OVERRUNS: Final = "SELECT pg_sleep(1)"


async def counted(database: Any, query: str = COUNT) -> int:
    async with database.reading(TENANT) as conn:
        tasks: int = await conn.fetchval(query)
    return tasks


async def created(database: Database, title: str) -> None:
    with acting_as(THE_SUITE):
        _ = await database.store(TENANT).create(CreateTaskBody(title=title))


async def created_elsewhere(database: Any) -> None:
    conn = await database.dedicated()
    try:
        async with conn.transaction():
            await conn.execute(SET_TENANT_AND_ACTING, TENANT, *acting(THE_SUITE, None, None))
            await conn.execute(INSERT, TENANT, "elsewhere")
    finally:
        await conn.close()


async def test_a_snapshot_sees_no_write_that_another_connection_commits_after_it_opened(
    database: Database,
) -> None:
    await created(database, "before")
    async with snapshotted() as scope:
        before = await counted(database)
        await created_elsewhere(database)
        again = await counted(database, COUNT_AGAIN)
        await scope.end()
        after = await counted(database)

    assert (before, again, after) == (1, 1, 2)


async def test_a_read_in_a_snapshot_sees_a_write_that_the_request_made_through_scoped(
    database: Database,
) -> None:
    async with snapshotted():
        before = await counted(database)
        await created(database, "in the request")
        after = await counted(database)

    assert (before, after) == (0, 1)


async def test_a_write_in_a_snapshot_is_refused(database: Any) -> None:
    async with snapshotted():
        with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
            async with database.reading(TENANT) as conn:
                await conn.execute(INSERT, TENANT, "refused")
        assert await counted(database) == 0


async def test_a_write_through_reading_outside_a_get_request_is_refused(database: Any) -> None:
    with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
        async with database.reading(TENANT) as conn:
            await conn.execute(INSERT, TENANT, "refused")


async def test_a_write_on_a_connection_of_its_own_is_seen_after_written(database: Any) -> None:
    async with snapshotted():
        before = await counted(database)
        with acting_as(THE_SUITE):
            async with database.connection() as conn, conn.transaction():
                await conn.execute(SET_TENANT_AND_ACTING, TENANT, *acting(THE_SUITE, None, None))
                await conn.execute(INSERT, TENANT, "on a connection of its own")
        unseen = await counted(database)
        await database.written(TENANT)
        seen = await counted(database)

    assert (before, unseen, seen) == (0, 0, 1)


async def test_a_request_reads_more_tenants_than_its_pool_holds(provisioned: Provisioned) -> None:
    database = PostgresDatabase(
        dsn=provisioned.app_dsn,
        schema=provisioned.schema,
        draws=replace(ONE_PROCESS, pool=2),
        kinds=kinds,
        timeouts=Timeouts(acquire=1.0),
    )
    try:
        async with snapshotted():
            for tenant in ("acme", "beta", "gamma", "beta", "acme"):
                async with database.reading(tenant) as conn:
                    assert await conn.fetchval(COUNT) == 0
    finally:
        await database.close()


async def test_a_part_past_its_bound_ends_its_snapshot_before_its_connection_goes_back(
    database: Any, asyncio_reports: Reported, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(READS_PART_BOUND_SECONDS_ENV, str(PART_BOUND_SECONDS))
    router = APIRouter()

    @router.get("/overruns", openapi_extra=PART)
    async def overruns() -> dict[str, str]:
        async with database.reading(TENANT) as conn:
            await conn.fetchval(OVERRUNS)
        return {"overruns": "answered"}

    app = await asyncio.to_thread(assembled, public.router, router, database=database)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        groups = [await client.get("/reads", params={"part": "/overruns"}) for _ in range(GROUPS)]

    assert [group.json()["parts"][0]["reason"] for group in groups] == ["timed out"] * GROUPS
    assert asyncio_reports.failures() == []
