"""A unit of work holds at most one connection of a database, and runs each block on it. A GET
request reads one snapshot on it, and sends each statement once. `docs/adr/template/0033` and
`0034`."""

import asyncio
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import log
from app.pacing import yield_now
from app.store import units
from app.store.loads import loader_of, loading
from app.store.units import BlockInsideABlock, HeldAcrossAWait, Holder, Need
from app.waits import SharedWork, beside, waiting
from tests.conftest import Logged


class Unanswerable(LookupError):
    pass


class NotFound(LookupError):
    pass


class Lost(Exception):
    pass


@dataclass(eq=False)
class Pool:
    """A pool that lends connections that record what they send, and a database of its own."""

    sent: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    said: list[str] = field(default_factory=list)
    holders: list[Holder] = field(default_factory=list)
    takes: int = 0
    lent: int = 0
    most_lent: int = 0
    running: int = 0
    most_running: int = 0
    released: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def ends(self) -> tuple[type[BaseException], ...]:
        return (Unanswerable,)

    async def taken(self, holder: Holder) -> "Conn":
        self.takes += 1
        self.lent += 1
        self.most_lent = max(self.most_lent, self.lent)
        self.holders.append(holder)
        return Conn(self)

    async def given_back(self, conn: Any, holder: Holder) -> None:
        assert (conn.pool, holder in self.holders) == (self, True)
        self.lent -= 1

    def lost(self, conn: Any, failed: BaseException) -> BaseException | None:
        return Lost(str(failed)) if conn.ended else None


@dataclass
class Conn:
    pool: Pool
    ended: bool = False

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self.pool.sent.append((query, args))
        self.pool.running += 1
        self.pool.most_running = max(self.pool.most_running, self.pool.running)
        try:
            if query == "ends":
                self.ended = True
                raise Unanswerable(query)
            if query == "unanswerable":
                raise Unanswerable(query)
            if query == "held":
                await self.pool.released.wait()
            await yield_now()
            return [query, *args]
        finally:
            self.pool.running -= 1


@asynccontextmanager
async def transaction(conn: Conn, whose: str, kind: str) -> AsyncGenerator[Conn]:
    conn.pool.said.append(f"begin {kind} {whose}")
    try:
        yield conn
    except BaseException:
        conn.pool.said.append("rollback")
        raise
    conn.pool.said.append("commit")


def reading(pool: Pool, whose: str = "t", *, enforced: bool = True) -> Any:
    def alone_in(conn: Conn) -> AbstractAsyncContextManager[Conn]:
        return transaction(conn, whose, "read")

    alone: tuple[Need, units.Opened] = (Need("read", whose), alone_in)
    if not units.reads_a_snapshot():
        return units.block(pool, pool, *alone, enforced=enforced)

    async def switched(conn: Conn) -> None:
        conn.pool.said.append(f"switch {whose}")

    return units.block(
        pool,
        pool,
        Need("snapshot", whose),
        lambda conn: transaction(conn, whose, "snapshot"),
        switched=switched,
        alone=alone,
        enforced=enforced,
    )


def scoped(pool: Pool, whose: str = "t", *, enforced: bool = True) -> Any:
    return units.block(
        pool,
        pool,
        Need("write", whose),
        lambda conn: transaction(conn, whose, "write"),
        enforced=enforced,
    )


async def fetched(pool: Pool, query: str, *args: Any, whose: str = "t") -> Any:
    async with reading(pool, whose) as conn:
        return await conn.fetch(query, *args)


def a_get() -> AbstractAsyncContextManager[units.Unit]:
    return units.unit(lambda: "GET /things", reads_a_snapshot=True)


def a_post() -> AbstractAsyncContextManager[units.Unit]:
    return units.unit(lambda: "POST /things", reads_a_snapshot=False)


async def test_a_get_reads_one_snapshot_on_one_connection_and_sends_each_statement_once() -> None:
    pool = Pool()
    async with a_get():
        first = await fetched(pool, "a", 1)
        again = await fetched(pool, "a", 1)
        other = await fetched(pool, "a", 2)

    assert (first, again, other) == (["a", 1], ["a", 1], ["a", 2])
    assert pool.sent == [("a", (1,)), ("a", (2,))]
    assert pool.said == ["begin snapshot t", "commit"]
    assert (pool.takes, pool.lent) == (1, 0)


async def test_a_kept_list_of_rows_is_handed_out_as_a_copy() -> None:
    pool = Pool()
    async with a_get():
        first = await fetched(pool, "a")
        first.append("changed")
        assert await fetched(pool, "a") == ["a"]


async def test_blocks_at_once_take_turns_on_the_one_connection_of_the_unit() -> None:
    pool = Pool()
    async with a_post(), asyncio.TaskGroup() as group:
        for n in range(5):
            _ = group.create_task(fetched(pool, "a", n))

    assert (pool.takes, pool.most_lent, pool.most_running) == (1, 1, 1)
    assert len(pool.sent) == 5


async def test_outside_a_unit_each_block_takes_a_connection_of_its_own() -> None:
    pool = Pool()
    _ = await fetched(pool, "a")
    _ = await fetched(pool, "a")

    assert (pool.takes, pool.lent) == (2, 0)
    assert pool.said == ["begin read t", "commit", "begin read t", "commit"]


async def test_a_write_ends_the_snapshot_first_and_the_next_read_opens_a_new_one() -> None:
    pool = Pool()
    async with a_get():
        _ = await fetched(pool, "a")
        async with scoped(pool):
            pass
        _ = await fetched(pool, "a")

    assert pool.takes == 1
    assert pool.sent == [("a", ()), ("a", ())]
    assert pool.said == [
        "begin snapshot t",
        "commit",
        "begin write t",
        "commit",
        "begin snapshot t",
        "commit",
    ]


async def test_a_read_of_another_tenant_points_the_snapshot_at_it_on_the_same_connection() -> None:
    pool = Pool()
    async with a_get():
        for whose in ("a", "b", "a", "b"):
            _ = await fetched(pool, "x", whose=whose)

    assert pool.takes == 1
    assert pool.said == ["begin snapshot a", "switch b", "switch a", "switch b", "commit"]
    assert pool.sent == [("x", ()), ("x", ())]


async def test_a_read_that_fails_in_the_database_ends_the_snapshot_and_raises_in_its_caller() -> (
    None
):
    pool = Pool()
    async with a_get():
        _ = await fetched(pool, "a")
        with pytest.raises(Unanswerable):
            _ = await fetched(pool, "unanswerable")
        assert await fetched(pool, "a") == ["a"]

    assert pool.said == ["begin snapshot t", "rollback", "begin snapshot t", "commit"]
    assert pool.takes == 1


async def test_a_failure_that_is_not_the_database_s_keeps_the_snapshot() -> None:
    pool = Pool()
    async with a_get():
        with pytest.raises(NotFound):
            async with reading(pool) as conn:
                _ = await conn.fetch("a")
                raise NotFound("a")
        _ = await fetched(pool, "a")

    assert pool.said == ["begin snapshot t", "commit"]
    assert pool.sent == [("a", ())]


async def test_a_unit_that_does_not_read_a_snapshot_runs_each_block_in_a_transaction_of_its_own() -> (
    None
):
    pool = Pool()
    async with a_post():
        async with scoped(pool):
            pass
        _ = await fetched(pool, "a")
        _ = await fetched(pool, "a")

    assert pool.takes == 1
    assert pool.said == [
        "begin write t",
        "commit",
        "begin read t",
        "commit",
        "begin read t",
        "commit",
    ]


async def test_a_block_inside_a_block_of_the_same_task_shares_its_connection_and_transaction() -> (
    None
):
    pool = Pool()
    async with a_post(), scoped(pool) as outer:
        async with reading(pool) as inner:
            assert inner is outer
        async with scoped(pool) as again:
            assert again is outer

    assert pool.takes == 1
    assert pool.said == ["begin write t", "commit"]


def scoped_a(pool: Pool) -> Any:
    return scoped(pool, "a")


def scoped_b(pool: Pool) -> Any:
    return scoped(pool, "b")


def bare(pool: Pool) -> Any:
    return units.block(pool, pool, Need("bare"), enforced=True)


@pytest.mark.parametrize(
    ("outer", "inner"),
    [(reading, scoped), (scoped_a, scoped_b), (bare, reading)],
)
async def test_a_block_whose_transaction_the_outer_block_cannot_share_is_refused(
    outer: Any, inner: Any
) -> None:
    pool = Pool()
    async with a_get(), outer(pool):
        with pytest.raises(BlockInsideABlock, match="cannot run it"):
            async with asyncio.timeout(1), inner(pool):
                pass

    assert pool.takes == 1


async def test_a_block_in_a_task_that_a_block_started_is_refused() -> None:
    pool = Pool()
    async with a_get(), reading(pool):
        task = asyncio.get_running_loop().create_task(fetched(pool, "a"))
        with pytest.raises(BlockInsideABlock, match="would wait for ever"):
            async with asyncio.timeout(1):
                await task


async def test_a_database_that_does_not_enforce_lends_a_second_connection_and_reports_it(
    logged: Logged,
) -> None:
    log.configure(None)
    pool = Pool()
    async with a_get(), reading(pool, enforced=False), scoped(pool, enforced=False):
        assert pool.lent == 2

    assert (pool.takes, pool.lent) == (2, 0)
    reported = [one for one in logged() if one["message"] == "unit of work would refuse"]
    assert [(one["route"], one["refusal"]) for one in reported] == [
        ("GET /things", "BlockInsideABlock")
    ]
    assert reported[0]["code.function.name"].endswith(
        "test_a_database_that_does_not_enforce_lends_a_second_connection_and_reports_it"
    )


async def test_a_wait_gives_the_connection_back_and_the_next_read_opens_a_new_snapshot() -> None:
    pool = Pool()
    async with a_get():
        _ = await fetched(pool, "a")
        async with waiting(1, on="outside.call"):
            assert pool.lent == 0
        _ = await fetched(pool, "a")

    assert pool.takes == 2
    assert pool.sent == [("a", ()), ("a", ())]
    assert pool.said == ["begin snapshot t", "commit", "begin snapshot t", "commit"]


async def test_a_wait_inside_a_block_is_refused() -> None:
    pool = Pool()
    async with a_get(), reading(pool):
        with pytest.raises(HeldAcrossAWait, match="outside.call"):
            async with waiting(1, on="outside.call"):
                pass


async def test_a_database_that_does_not_enforce_holds_across_a_wait_inside_a_block_and_reports_it(
    logged: Logged,
) -> None:
    log.configure(None)
    pool = Pool()
    async with a_get(), reading(pool, enforced=False), waiting(1, on="outside.call"):
        assert pool.lent == 1

    [reported] = [one for one in logged() if "refusal" in one]
    assert reported["refusal"] == "HeldAcrossAWait"
    assert reported["code.function.name"].startswith("tests.store.test_units.")


async def test_work_beside_the_requests_is_a_unit_of_its_own() -> None:
    pool = Pool()
    async with a_get():
        _ = await fetched(pool, "a")
        _ = await beside(fetched(pool, "b"), name="reads beside", within=1)

    assert pool.takes == 2
    assert pool.said == ["begin snapshot t", "begin read t", "commit", "commit"]
    assert pool.holders[1].unit() == "beside reads beside"


async def test_a_turn_of_a_loader_reads_on_the_callers_unit_and_other_shared_work_on_its_own() -> (
    None
):
    pool = Pool()
    lent_when_joined: list[int] = []

    async def keyed(conn: Any, keys: Sequence[int]) -> Mapping[int, Any]:
        return {key: await conn.fetch("keyed", key) for key in keys}

    async def shared_read() -> Any:
        lent_when_joined.append(pool.lent)
        return await fetched(pool, "a")

    shared = SharedWork[str, Any]("reads in shared work")
    with loading():
        async with a_get():
            loader = loader_of("t", lambda: reading(pool))
            assert await loader.load(keyed, 1) == ["keyed", 1]
            assert pool.lent == 1
            _ = await shared.joined("a", shared_read)

    assert lent_when_joined == [0]
    assert pool.takes == 2
    assert pool.said == ["begin snapshot t", "commit", "begin read t", "commit"]
    assert [holder.unit() for holder in pool.holders] == [
        "GET /things",
        "shared reads in shared work",
    ]


async def test_a_wait_for_a_turn_of_a_loader_inside_a_block_is_refused() -> None:
    pool = Pool()

    async def doubled(_conn: Any, keys: Sequence[int]) -> Mapping[int, int]:
        return {key: key * 2 for key in keys}

    with loading():
        async with a_get(), reading(pool):
            with pytest.raises(BlockInsideABlock, match="a wait for a turn"):
                async with asyncio.timeout(1):
                    _ = await loader_of("t", lambda: reading(pool)).load(doubled, 1)


async def test_a_connection_that_ends_is_given_back_and_the_next_block_takes_another() -> None:
    pool = Pool()
    async with a_get():
        _ = await fetched(pool, "a")
        with pytest.raises(Lost):
            _ = await fetched(pool, "ends")
        assert pool.lent == 0
        _ = await fetched(pool, "a")

    assert pool.takes == 2


async def test_a_cancelled_block_gives_its_connection_back() -> None:
    pool = Pool()
    async with a_get():
        reads = asyncio.get_running_loop().create_task(fetched(pool, "held"))
        while not pool.sent:
            await yield_now()
        _ = reads.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reads
        assert pool.lent == 0
        assert pool.said == ["begin snapshot t", "rollback"]


async def test_giving_the_connection_back_waits_for_the_block_in_flight() -> None:
    pool = Pool()
    async with a_get() as unit:
        reads = asyncio.get_running_loop().create_task(fetched(pool, "held"))
        while not pool.sent:
            await yield_now()
        gives_back = asyncio.get_running_loop().create_task(unit.give_back())
        await yield_now()
        assert not gives_back.done()
        pool.released.set()
        await gives_back
        assert await reads == ["held"]
        assert pool.lent == 0

    assert pool.said == ["begin snapshot t", "commit"]


async def test_a_holder_names_its_unit_and_the_code_of_its_latest_block() -> None:
    pool = Pool()
    async with a_get():
        _ = await fetched(pool, "a")
        holder = pool.holders[0]
        assert holder.unit() == "GET /things"
        assert holder.function == "tests.store.test_units.fetched"
        assert holder.block_ended is not None


async def test_an_argument_that_cannot_be_kept_is_refused() -> None:
    pool = Pool()
    async with a_get():
        with pytest.raises(TypeError, match="cannot be kept"):
            _ = await fetched(pool, "a", bytearray(b"not keepable"))


async def test_an_argument_is_kept_by_its_type_as_well_as_its_value() -> None:
    pool = Pool()
    async with a_get():
        for argument in (1, True, [1, 2], (1, 2), {"b": [1]}, {"b": [1]}, {1, 2}, {2, 1}):
            _ = await fetched(pool, "a", argument)

    assert len(pool.sent) == 6


async def test_a_statement_whose_answer_changes_in_a_transaction_is_sent_each_time() -> None:
    pool = Pool()
    async with a_get():
        for query in ("SELECT clock_timestamp()",) * 2 + ("SELECT now()",) * 2:
            _ = await fetched(pool, query)

    assert [query for query, _ in pool.sent] == [
        "SELECT clock_timestamp()",
        "SELECT clock_timestamp()",
        "SELECT now()",
    ]


class LentAtTheStart:
    """Records how many connections were lent when each response started."""

    def __init__(self, app: ASGIApp, pool: Pool, seen: list[int]) -> None:
        self._app: ASGIApp = app
        self._pool: Pool = pool
        self._seen: list[int] = seen

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def started(message: Message) -> None:
            if message["type"] == "http.response.start":
                self._seen.append(self._pool.lent)
            await send(message)

        await self._app(scope, receive, started)


def test_each_request_is_a_unit_that_gives_its_connection_back_before_its_response_starts() -> None:
    pool = Pool()
    seen: list[int] = []
    app = FastAPI()
    units.instrument(app)
    app.add_middleware(LentAtTheStart, pool=pool, seen=seen)

    @app.api_route("/read", methods=["GET", "POST"])
    async def route() -> int:
        _ = await fetched(pool, "a")
        _ = await fetched(pool, "a")
        return pool.lent

    with TestClient(app) as client:
        assert client.get("/read").json() == 1
        assert client.post("/read").json() == 1

    assert seen == [0, 0]
    assert pool.takes == 2
    assert pool.said == ["begin snapshot t", "commit", *["begin read t", "commit"] * 2]
    assert [holder.unit() for holder in pool.holders] == ["GET /read", "POST /read"]


def test_a_body_that_streams_reads_on_a_connection_of_its_own_for_each_block() -> None:
    pool = Pool()
    lent: list[int] = []
    app = FastAPI()
    units.instrument(app)

    async def body() -> AsyncGenerator[bytes]:
        for _ in range(2):
            async with reading(pool) as conn:
                lent.append(pool.lent)
                yield str(await conn.fetch("a")).encode()
            lent.append(pool.lent)

    @app.get("/streams")
    async def streams() -> StreamingResponse:
        _ = await fetched(pool, "first")
        return StreamingResponse(body())

    with TestClient(app) as client:
        assert client.get("/streams").status_code == 200

    assert lent == [1, 0, 1, 0]
    assert pool.takes == 3
    assert pool.said == ["begin snapshot t", "commit", *["begin read t", "commit"] * 2]


def test_a_block_open_in_a_dependency_when_the_response_starts_gives_its_connection_back_after() -> (
    None
):
    pool = Pool()
    app = FastAPI()
    units.instrument(app)

    async def read_for_the_request() -> AsyncGenerator[Any]:
        async with reading(pool) as conn:
            yield conn

    @app.get("/held")
    async def held(conn: Annotated[Any, Depends(read_for_the_request)]) -> int:
        _ = await conn.fetch("a")
        return pool.lent

    with TestClient(app) as client:
        assert client.get("/held", timeout=5).json() == 1

    assert (pool.takes, pool.lent) == (1, 0)
    assert pool.said == ["begin snapshot t", "commit"]


async def test_a_task_that_outlives_its_unit_reads_on_a_connection_of_its_own() -> None:
    pool = Pool()
    ended = asyncio.Event()

    async def reads_after_the_unit() -> Any:
        _ = await ended.wait()
        return await fetched(pool, "late")

    async with a_post():
        _ = await fetched(pool, "a")
        late = asyncio.get_running_loop().create_task(reads_after_the_unit())
    ended.set()

    assert await late == ["late"]
    assert (pool.takes, pool.lent) == (2, 0)


def test_a_body_that_streams_while_a_dependency_holds_a_block_ends_and_gives_it_back() -> None:
    pool = Pool()
    app = FastAPI()
    units.instrument(app)

    async def read_for_the_request() -> AsyncGenerator[Any]:
        async with reading(pool) as conn:
            yield conn

    @app.get("/streams")
    async def streams(conn: Annotated[Any, Depends(read_for_the_request)]) -> StreamingResponse:
        _ = await conn.fetch("a")
        return StreamingResponse(iter([b"streamed"]))

    with TestClient(app) as client:
        assert client.get("/streams", timeout=5).content == b"streamed"

    assert (pool.takes, pool.lent) == (1, 0)


async def test_a_unit_that_is_done_reads_inside_its_open_block_on_the_connection_of_the_block() -> (
    None
):
    pool = Pool()
    async with a_post() as unit, scoped(pool):
        await unit.finish()
        _ = await fetched(pool, "a")
        assert pool.lent == 1

    assert (pool.takes, pool.most_lent, pool.lent) == (1, 1, 0)
