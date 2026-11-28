"""A unit of work: what one request, one attempt of a job, one run of shared work, or one task of
work beside the requests does on the database. `docs/adr/template/0034`.

The boundary opens the unit: `UnitPerRequest` for each HTTP request, `app/worker.py` for each
attempt of a job, and `app.waits` for each run of shared work and each task of work beside the
requests. A store reaches the database only through the unit, and the unit holds at most `HOLDS`
connection of each database at a time. So a unit never waits for a second connection while it
holds one, and a pool of any size cannot lock.

A block takes a connection from the pool when it starts, and gives it back when it ends. So a
unit that waits between two blocks holds no connection, whatever it waits on. The one exception
is a GET or HEAD request, which reads one snapshot: it holds its connection between blocks,
until it ends, its response starts, or `return_the_connection` runs before a wait.

Each use is a block. The blocks of a unit run one at a time. A block inside a
block of the same task runs on the connection of the outer block, in the transaction of the
outer block. A block whose transaction the outer block cannot share, and a block in a task that
a block started, raise `BlockInsideABlock`: each would wait for ever for the outer block. Outside
every unit, as in the lifespan, a loop or a test, each block takes a connection of its own and
gives it back when it ends.

A database that does not enforce the unit lends a second connection to a block that
`BlockInsideABlock` would refuse, and holds its connection across a wait inside a block.
`log.unit_would_refuse` names each. `UNIT_OF_WORK_MODE=evaluate` sets this.
"""

import asyncio
import inspect
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Hashable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import FrameType
from typing import Any, Final, Literal, Protocol

from fastapi import FastAPI, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import log
from app.loop_watch import is_own, qualified
from app.store.snapshots import Kept

HOLDS: Final = 1
"""The most connections of one database that a unit of work holds at a time."""

READ_METHODS: Final = frozenset({"GET", "HEAD"})
"""The methods whose requests read through a snapshot. `docs/adr/template/0033`."""

PASSED_THROUGH: Final = ("app.store.", "app.waits")
"""The modules that a block or a wait passes through on its way from the code that asks for it."""

type Opened = Callable[[Any], AbstractAsyncContextManager[Any]]
"""A transaction on a connection: what a block runs in, from its start to its end."""

type Switched = Callable[[Any], Awaitable[None]]
"""What points the snapshot open on a connection at another tenant."""

type Kind = Literal["bare", "read", "write", "snapshot"]


class BlockInsideABlock(RuntimeError):
    """A block of a unit of work started inside a block of the same unit that it cannot share:
    in a task that the outer block started, or in a transaction that cannot run it. The second
    block would wait for ever for the connection that the first one holds."""


class HeldAcrossAWait(RuntimeError):
    """A wait on something other than the database started inside a block, in a unit of work or
    outside every unit, so the block would hold its connection for the whole wait."""


@dataclass(eq=False)
class Holder:
    """Who holds one lent connection, and since when, on this process's `time.monotonic`
    clock: the unit, the code of its latest block, when the connection was taken, when the
    latest block ended, and when the connection went back. `releasing` while it goes back."""

    unit: Callable[[], str | None]
    owner: object
    """What holds the connection: the unit's hold of one database, or one block outside a unit."""
    function: str | None
    line: int | None
    acquired: float
    block_ended: float | None = None
    released: float | None = None
    releasing: bool = False

    def held_ms(self, now: float) -> float:
        """The milliseconds from when the connection was taken to `now`."""
        return round((now - self.acquired) * 1000, 1)

    def idle_ms(self, now: float) -> float | None:
        """The milliseconds from the end of the latest block to `now`, `None` while one runs."""
        return None if self.block_ended is None else round((now - self.block_ended) * 1000, 1)

    def held(self, now: float) -> dict[str, str | int | float | None]:
        """What `log.acquire_timed_out` says of this holder at `now`, as `log.connection_held`
        names it."""
        return {
            "route": self.unit(),
            "code.function.name": self.function,
            "code.line.number": self.line,
            "held_ms": self.held_ms(now),
            "idle_ms": self.idle_ms(now),
        }


class Lender(Protocol):
    """How a database lends one connection of its pool, and takes it back."""

    @property
    def ends(self) -> tuple[type[BaseException], ...]:
        """What a statement raises when the database fails it, or when the connection ends."""
        ...

    async def taken(self, holder: Holder) -> Any:
        """A connection from the pool, which `holder` holds."""
        ...

    async def given_back(self, conn: Any, holder: Holder) -> None:
        """Give `conn` back to the pool, or end it when it ended while lent."""
        ...

    def lost(self, conn: Any, failed: BaseException) -> BaseException | None:
        """What `failed` means when `conn` ended with it, and `None` when `conn` did not end."""
        ...


@dataclass(frozen=True)
class Need:
    """What a block needs of the connection: `bare`, no transaction of the unit's; `read` or
    `write`, a transaction of its own for `whose`; or `snapshot`, the snapshot of the unit, of
    `whose`."""

    kind: Kind
    whose: Hashable | None = None

    def shares(self, outer: "Need") -> bool:
        """Whether a block that needs this can run in the transaction of a block that needs
        `outer`: a bare block in any block, a read of the same `whose` in a block that is not
        bare, and a write of the same `whose` in a write."""
        if outer.kind == "bare" or self.kind == "bare":
            return self.kind == "bare"
        return self.whose == outer.whose and (self.kind != "write" or outer.kind == "write")


_UNIT: ContextVar["Unit | None"] = ContextVar("unit_of_work", default=None)

_INSIDE: ContextVar[tuple["Held", ...]] = ContextVar("inside_a_block", default=())


class Unit:
    """One unit of work, opened by `unit`. `route` names it: a request's method and route, a
    job's kind, or the name of a run of shared work or of work beside the requests."""

    def __init__(self, route: Callable[[], str | None], *, reads_a_snapshot: bool) -> None:
        self._route: Final = route
        self.reads_a_snapshot: Final = reads_a_snapshot
        self._held: Final[dict[Hashable, Held]] = {}
        self.done: bool = False
        """Whether the unit gave its connections back for good: its response started, or it
        ended. Each block after it, as in a body that streams or a task that outlives the unit,
        runs on a connection of its own, which goes back when the block ends, and reads no
        snapshot."""

    def route(self) -> str | None:
        return self._route()

    def held(self, database: Hashable, lender: Lender, *, enforced: bool) -> "Held":
        """What this unit holds of `database`, made on first use."""
        if database not in self._held:
            self._held[database] = Held(
                lender,
                self._route,
                owner=self,
                enforced=enforced,
                holds_between_blocks=self.reads_a_snapshot,
            )
        return self._held[database]

    async def finish(self) -> None:
        """Mark the unit as `done`, and give each connection back. Raises as `give_back`
        raises."""
        self.done = True
        await self.give_back()

    async def give_back(self) -> None:
        """End the snapshot of each database, and give each connection back. Waits for the
        block in flight of another task. A block of the running task gives its connection back
        when it ends. Tries each database, then raises what failed."""
        async with AsyncExitStack() as each:
            for held in self._held.values():
                each.push_async_callback(held.give_back)


def current() -> Unit | None:
    """The unit of work of the running task, or `None` outside every unit."""
    return _UNIT.get()


def reads_a_snapshot() -> bool:
    """Whether a read of a tenant in the running task reads the snapshot of its unit of work."""
    opened = _UNIT.get()
    return opened is not None and opened.reads_a_snapshot and not opened.done


@asynccontextmanager
async def unit(route: Callable[[], str | None], *, reads_a_snapshot: bool) -> AsyncGenerator[Unit]:
    """Run the block as one unit of work, named by `route`, which gives back what it holds when
    it ends. In a unit that `reads_a_snapshot`, each read of a tenant reads the snapshot. A task
    started inside the block, such as an item of an `asyncio.TaskGroup`, is in the unit."""
    opened = Unit(route, reads_a_snapshot=reads_a_snapshot)
    token = _UNIT.set(opened)
    inside = _INSIDE.set(())
    try:
        yield opened
    finally:
        _INSIDE.reset(inside)
        _UNIT.reset(token)
        await opened.finish()


def within[T](work: Awaitable[T], route: str) -> Coroutine[Any, Any, T]:
    """`work`, run as a unit of work of its own, named `route`."""

    async def run() -> T:
        async with unit(lambda: route, reads_a_snapshot=False):
            return await work

    return run()


async def return_the_connection(on: str) -> None:
    """Give back the connection that a GET or HEAD request holds, and end its snapshot, before the
    request waits on `on`. A read after it opens a new snapshot. Any other unit of work holds no
    connection between blocks, so it gives back nothing.

    Raises `HeldAcrossAWait` inside a block, in a unit or outside every unit: the block holds the
    connection until it ends. A database that does not enforce the unit holds it across the wait,
    and `log.unit_would_refuse` names it."""
    opened = _UNIT.get()
    inside = _INSIDE.get()
    if inside:
        route = (lambda: None) if opened is None else opened.route
        of = "" if opened is None else f" of {opened.route()!r}"
        refused = HeldAcrossAWait(
            f"{on} started inside a block{of}, which holds a connection for the whole wait. "
            f"End the block before the wait."
        )
        if any(held.enforced for held in inside):
            raise refused
        _would_refuse(route, refused)
        return
    if opened is not None and opened.reads_a_snapshot:
        await opened.give_back()


def refuse_inside_a_block(what: str) -> None:
    """Raise `BlockInsideABlock`, which names `what`, inside a block of a unit of work. A database
    that does not enforce the unit lets it pass, and `log.unit_would_refuse` names it."""
    inside = _INSIDE.get()
    if not inside:
        return
    refused = BlockInsideABlock(
        f"{what} started inside a block, and would wait for ever for the connection that the "
        f"block holds. End the block before it starts."
    )
    if any(held.enforced for held in inside):
        raise refused
    opened = _UNIT.get()
    _would_refuse((lambda: None) if opened is None else opened.route, refused)


def _would_refuse(route: Callable[[], str | None], refused: Exception) -> None:
    function, line = _code()
    log.unit_would_refuse(
        route=route(), refusal=type(refused).__name__, function=function, line=line
    )


def _code() -> tuple[str | None, int | None]:
    """The function and line of the innermost frame of this project's own code outside
    `PASSED_THROUGH`: the code that uses the database, or that waits."""
    frame: FrameType | None = inspect.currentframe()
    while frame is not None:
        if is_own(frame) and not str(frame.f_globals.get("__name__", "")).startswith(
            PASSED_THROUGH
        ):
            return qualified(frame), frame.f_lineno
        frame = frame.f_back
    return None, None


@dataclass
class _Block:
    need: Need
    used: Any


@dataclass
class _Open:
    """The snapshot open on the connection: its transaction, the tenant it reads now, and the
    answers it kept for each tenant."""

    stack: AsyncExitStack
    whose: Hashable
    kept: dict[Hashable, Kept] = field(default_factory=dict)


class Held:
    """The connection that one unit of work holds of one database, and the snapshot open on it.
    `block` makes one for each block outside every unit. Unless it `holds_between_blocks`, the
    connection goes back to the pool when each block ends."""

    def __init__(
        self,
        lender: Lender,
        route: Callable[[], str | None],
        *,
        owner: object,
        enforced: bool,
        holds_between_blocks: bool = False,
    ) -> None:
        self._lender: Final = lender
        self._route: Final = route
        self._owner: Final = owner
        self.enforced: Final = enforced
        self._holds_between_blocks: Final = holds_between_blocks
        self._conn: Any = None
        self._holder: Holder | None = None
        self._turn: Final = asyncio.Lock()
        self._task: asyncio.Task[Any] | None = None
        self._blocks: Final[list[_Block]] = []
        self._open: _Open | None = None
        self._given_back_when_done: bool = False

    def refusal(self, need: Need) -> BlockInsideABlock | None:
        """Why a block that needs `need` cannot start on this connection now, and `None` when
        it can."""
        if self not in _INSIDE.get():
            return None
        if self._task is not asyncio.current_task():
            return BlockInsideABlock(
                f"a {need.kind} block started in a task that a block of {self._route()!r} "
                f"started, and would wait for ever for that block. Start the task after the "
                f"block ends."
            )
        outer = self._blocks[-1].need
        if need.shares(outer):
            return None
        return BlockInsideABlock(
            f"a {need.kind} block of {need.whose!r} started inside a {outer.kind} block of "
            f"{outer.whose!r} in {self._route()!r}, whose transaction cannot run it. End the "
            f"outer block first."
        )

    @asynccontextmanager
    async def block(
        self,
        need: Need,
        opened: Opened | None = None,
        switched: Switched | None = None,
    ) -> AsyncGenerator[Any]:
        """The connection in what `need` names, one block at a time: as is for `bare`, inside
        `opened(conn)` for `read` and `write`, and the snapshot for `snapshot`, which
        `opened(conn)` opens and `switched(conn)` points at another tenant. Raises
        `BlockInsideABlock` as `refusal` says. A block inside a block of the same task uses what
        the outer block used."""
        refused = self.refusal(need)
        if refused is not None:
            raise refused
        if self in _INSIDE.get():
            async with self._nested(need) as used:
                yield used
            return
        async with self._turn:
            self._task = asyncio.current_task()
            inside = _INSIDE.set((*_INSIDE.get(), self))
            try:
                async with self._used(need, opened, switched) as used:
                    self._blocks.append(_Block(need, used))
                    try:
                        yield used
                    finally:
                        _ = self._blocks.pop()
            finally:
                _INSIDE.reset(inside)
                await self._ended_a_block()

    async def _ended_a_block(self) -> None:
        self._task = None
        if self._holder is not None:
            self._holder.block_ended = time.monotonic()
        if self._given_back_when_done or not self._holds_between_blocks:
            self._given_back_when_done = False
            await self._ended_and_given_back()

    @asynccontextmanager
    async def _nested(self, need: Need) -> AsyncGenerator[Any]:
        outer = self._blocks[-1]
        self._blocks.append(_Block(need, outer.used))
        try:
            yield outer.used
        finally:
            _ = self._blocks.pop()

    @asynccontextmanager
    async def _used(
        self, need: Need, opened: Opened | None, switched: Switched | None
    ) -> AsyncGenerator[Any]:
        conn = await self._lent()
        try:
            if need.kind == "snapshot":
                async with self._snapshot(conn, need.whose, opened, switched) as kept:
                    yield kept
                return
            await self._end_snapshot(None)
            if opened is None:
                yield conn
                return
            async with opened(conn) as inside:
                yield inside
        except self._lender.ends as failed:
            lost = self._lender.lost(conn, failed)
            if lost is None:
                raise
            await self._given_back()
            raise lost from failed
        except asyncio.CancelledError:
            await self._given_back()
            raise

    @asynccontextmanager
    async def _snapshot(
        self,
        conn: Any,
        whose: Hashable,
        opened: Opened | None,
        switched: Switched | None,
    ) -> AsyncGenerator[Kept]:
        if opened is None or switched is None:
            raise TypeError("a snapshot opens in `opened` and moves to a tenant in `switched`")
        try:
            if self._open is None:
                stack = AsyncExitStack()
                _ = await stack.enter_async_context(opened(conn))
                self._open = _Open(stack, whose)
            elif self._open.whose != whose:
                await switched(conn)
                self._open.whose = whose
            yield self._open.kept.setdefault(whose, Kept(conn))
        except BaseException as failed:
            if not isinstance(failed, Exception) or isinstance(failed, self._lender.ends):
                await self._end_snapshot(failed)
            raise

    async def _lent(self) -> Any:
        function, line = _code()
        if self._conn is not None and self._holder is not None:
            self._holder.function, self._holder.line = function, line
            return self._conn
        holder = Holder(self._route, self._owner, function, line, acquired=time.monotonic())
        self._conn = await self._lender.taken(holder)
        self._holder = holder
        return self._conn

    async def _end_snapshot(self, failed: BaseException | None) -> None:
        if self._open is None:
            return
        stack = self._open.stack
        self._open = None
        if failed is None:
            await stack.aclose()
        else:
            _ = await stack.__aexit__(type(failed), failed, failed.__traceback__)

    async def give_back(self) -> None:
        """End the snapshot, and give the connection back. Waits for the block in flight of
        another task. Inside a block, or in a task that a block started, the block gives it back
        when it ends."""
        if self in _INSIDE.get():
            self._given_back_when_done = True
            return
        async with self._turn:
            await self._ended_and_given_back()

    async def _ended_and_given_back(self) -> None:
        conn = self._conn
        try:
            await self._end_snapshot(None)
        except self._lender.ends as failed:
            lost = self._lender.lost(conn, failed)
            if lost is None:
                raise
            raise lost from failed
        finally:
            await self._given_back()

    async def _given_back(self) -> None:
        conn, holder = self._conn, self._holder
        self._conn = self._holder = None
        self._open = None
        if conn is not None and holder is not None:
            await self._lender.given_back(conn, holder)


@asynccontextmanager
async def block(
    database: Hashable,
    lender: Lender,
    need: Need,
    opened: Opened | None = None,
    *,
    switched: Switched | None = None,
    alone: tuple[Need, Opened] | None = None,
    enforced: bool,
) -> AsyncGenerator[Any]:
    """A block of `database`, which `lender` lends, as `Held.block` runs it: on the connection
    of the unit of work. A unit that is `done` runs a block there only inside a block of its own,
    which still holds the connection. Otherwise, and outside every unit, the block runs on a
    connection of its own, in `alone` when it names one, as a read in place of the snapshot.

    Raises `BlockInsideABlock` as `Held.refusal` says. When `enforced` is off, such a block runs on
    a connection of its own, and `log.unit_would_refuse` names it."""
    opened_unit = _UNIT.get()
    if opened_unit is not None:
        held = opened_unit.held(database, lender, enforced=enforced)
        if not opened_unit.done or held in _INSIDE.get():
            refused = held.refusal(need)
            if refused is None:
                async with held.block(need, opened, switched) as used:
                    yield used
                return
            if enforced:
                raise refused
            _would_refuse(opened_unit.route, refused)
    own, opening = (need, opened) if alone is None else alone
    held = Held(
        lender,
        (lambda: None) if opened_unit is None else opened_unit.route,
        owner=object(),
        enforced=enforced,
    )
    try:
        async with held.block(own, opening) as used:
            yield used
    finally:
        await held.give_back()


class UnitPerRequest:
    """The middleware `instrument` adds."""

    def __init__(self, app: ASGIApp) -> None:
        self._app: ASGIApp = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope)

        def route() -> str:
            return f"{request.method} {log.route_answering(request)}"

        async with unit(route, reads_a_snapshot=scope["method"] in READ_METHODS) as opened:

            async def giving_back(message: Message) -> None:
                if message["type"] == "http.response.start":
                    await opened.finish()
                await send(message)

            await self._app(scope, receive, giving_back)


def instrument(app: FastAPI) -> None:
    """Make each HTTP request that `app` serves one unit of work, which gives its connection
    back before its response starts. A block after that, as in a body that streams, runs on a
    connection of its own. A GET or HEAD request reads through a snapshot. A part of
    `GET /reads` is a request of its own, so it is a unit of its own. Call it after
    `request_events.instrument`, so the request event of a GET or HEAD request uses the
    connection that the request holds."""
    app.add_middleware(UnitPerRequest)
