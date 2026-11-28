# 0034. A unit of work holds one connection

Date: 2026-11-25

## Status

Accepted. It takes the option that
[0032](0032-a-request-s-reads-do-not-grow-with-its-rows.md) and
[0033](0033-a-get-request-reads-one-snapshot-and-sends-each-statement-once.md) left to a decision
of its own: one unit of work for the whole request.

## Context

A project generated from this template answered `503` with `AcquireTimedOut` 20 times on one day
and 8 times on another. One case was traced. A GET request read its snapshot, which holds a
connection until the response starts. Then a store of the project took a second connection, in a
transaction of its own. Nine such requests ran at once on a pool of 7. Each held one connection
and waited for a second, until `Timeouts.acquire` ended the wait. Two of them failed.

This is the condition that HikariCP calls pool locking. When `Tn` units of work each need `Cm`
connections at once, a pool of fewer than `Tn × (Cm − 1) + 1` connections can lock. A unit that
takes a second connection while it holds one needs a pool larger than the units that run at once.
No pool size is safe from a burst.

The gate could not find it. The project listed 7 GET routes in `MORE_THAN_ONE_CONNECTION`, each
with a reason, and the test did not read 86 of its GET routes at all. A reason on a list does not
make a second connection safe. Only a shape in which a unit cannot take one does.

Each request also took 3 connections in turn: one for the session check, one for the snapshot, and
one for the request event. Each connection that goes back to the pool costs one round trip, the
pool's reset. A typical GET of 160 ms made 19 round trips, and about 6 of them were its own reads.

A worker of the same project, with a pool of 7, timed out 35 times in 12 minutes. Each holder
was an attempt of a job that held a connection between two blocks while it waited on something
other than the database: an `asyncio.Event`, a backoff, or a workspace that took minutes to
start. No check sees such a wait.

## Decision

- **A unit of work is what one request, one attempt of a job, one run of shared work, or one task
  of work beside the requests does on the database.** `app/store/units.py` holds it. The boundary
  opens it: `UnitPerRequest` for each HTTP request, `run` in `app/worker.py` for each attempt and
  `_renew` for each renewal of its lease, and `_started` in `app/waits.py` for each run and each
  task. A part of `GET /reads` is a request,
  so it is a unit of its own. A turn of a loader runs in the unit of its callers, through
  `SharedWork(..., in_the_callers_unit=True)`.
- **A unit holds at most `HOLDS` connection of each database at a time.** `lent`, `connection`,
  `reading` and `scoped` of `PostgresDatabase` each run a block through `units.block`. `Pool` in
  `app/store/pg.py` is the one place that takes a connection from asyncpg and gives it back, and
  `PostgresDatabase` keeps it private. So no store can take a second connection in a unit.
  `Pool.taken` refuses a unit a second connection of one pool all the same, and
  `test_a_unit_that_asks_one_pool_for_a_second_connection_is_refused` holds it.
- **The blocks of a unit run one at a time.** The items of an `asyncio.TaskGroup` take turns. A
  block inside a block of the same task runs in the transaction of the outer block when
  `Need.shares` allows it: a read or a bare block inside a block of the same tenant, and a write
  inside a write. Any other block inside a block raises `BlockInsideABlock`, and
  so does a block in a task that a block started. Each would wait for ever for the outer block.
  `tests/store/test_units.py` holds each rule.
- **A GET or HEAD request reads one snapshot on its connection.**
  [0033](0033-a-get-request-reads-one-snapshot-and-sends-each-statement-once.md) says what a
  snapshot is and what it keeps. A read of another tenant points the snapshot at that tenant with
  one statement, `SET_TENANT_AND_ACTING`, in the same transaction. The snapshot keeps the answers of
  each tenant apart. A block that does not read the snapshot, such as a write, ends it first.
- **Outside a GET request, each `reading` and each `scoped` block opens a transaction of its
  own.** `scoped` opens one that
  [0017](0017-the-database-records-every-write-and-the-request-layer-every-request.md) records,
  and `reading` one that is READ ONLY. A request that writes is not one transaction.
- **Each block takes the connection from the pool when it starts, and gives it back when it
  ends.** So a unit that waits between two blocks holds no connection, whatever it waits on.
  `Held._ended_a_block` in `app/store/units.py` gives it back.
  `test_a_unit_that_waits_on_an_event_between_two_blocks_holds_no_connection` and
  `test_units_that_wait_between_two_blocks_share_one_connection_and_none_times_out` hold this.
- **A GET or HEAD request holds its connection between blocks, until it ends, its response
  starts, or it calls `units.return_the_connection`.** Its snapshot lives on that connection,
  and its session check, snapshot and request event share one reset. `UnitPerRequest` gives it
  back at `http.response.start`, after the request event. A block that is still open then, as in a
  dependency with `yield`, gives it back when it ends. After the start, each block takes a
  connection of its own. `units.return_the_connection` gives it back before a wait.
  `waits.waiting` calls it before its bound, `SharedWork.joined` before a caller joins a run of
  another unit, and `reads.answer` before the parts of a group start. A read after the wait opens
  a new snapshot.
- **A wait through `waits.waiting` inside a block raises `HeldAcrossAWait`**, in a unit and
  outside every unit, because the block holds the connection until it ends.
- **In a GET or HEAD request, the session check and the request event use the connection of the
  unit.** The identity seam runs before the tenant is known, so it reads in a bare block. The
  first read of the snapshot then sets the tenant and the actor on the same connection.
  `RecordsRequests` records the request event in a write block, which ends the snapshot first.
- **`waits.waiting` is the one door to a system outside the process.**
  `tests/waits/test_the_only_door.py` reads each module of `app/`. It fails on each use, in an
  `async def`, of a client from a module of `OUTSIDE` that is not inside `waiting`.
- **The pool names who holds each connection.** Each `Holder` names its unit, the function and the
  line of its latest block, and three times: when it took the connection, when its latest block
  ended, and when the connection went back. `connection held` names a holder while it holds a
  connection past `Timeouts.held`, as HikariCP's `leakDetectionThreshold` does.
  `DATABASE_HELD_REPORTED_AFTER` sets it. When no connection
  comes within `Timeouts.acquire`, `acquire timed out` names each holder with the time it held and
  the time since its latest block. It also counts the connections that go back to the pool and the
  ones that open. `db.client.connection.use_time` records how long each connection was lent.
- **`UNIT_OF_WORK_MODE=evaluate` reports where `enforce` refuses.** A block that `BlockInsideABlock`
  would refuse takes a connection of its own, and a wait inside a block holds the connection.
  `unit of work would refuse` names each. `enforce` is the default. A project whose code takes a
  second connection runs `evaluate` first, fixes each line that the log names, and then enforces.
- **No GET route holds more than `HOLDS` connection at once, and no list names one that may.**
  `tests/integration/test_routes_read_a_fixed_count.py` holds this for each GET route that it
  reads. `MORE_THAN_ONE_CONNECTION` is gone. The test holds itself to its purpose: under
  `evaluate`, a route that writes inside a read holds two.
- **As many units as the pool has connections do not lock it.**
  `test_as_many_units_as_the_pool_lends_each_read_then_write_and_none_waits_for_a_second` runs 3
  units on a pool of 3. Each reads its snapshot, waits until each unit holds a connection, then
  writes through a store call. Before this decision, 2 of the 3 raised `AcquireTimedOut`.

## Considered options

- **Refuse a second connection at run time, except on the routes that a list names.** A reason on
  the list does not make the second connection safe: `N` such units on a pool of `N` lock. A route
  that the gate does not read raises its first refusal in production.
- **One transaction for each request that writes, as Django's `ATOMIC_REQUESTS` does.** A failed
  statement aborts the transaction. An item that catches its own failure, as `AGENTS.md` asks,
  then makes each later statement fail. A savepoint for each block costs 2 round trips. A wait
  outside the process must give the connection back, which ends the transaction half done. It
  also changes what 0017 records and what `ConnectionLost` means. A project that needs several
  writes to commit together writes them in one `scoped`.
- **A snapshot for each tenant.** One connection holds one transaction at a time. A new snapshot
  costs `BEGIN`, `SET_TENANT_AND_ACTING` and `COMMIT`, where pointing the open one at another tenant
  costs one statement.
- **Keep the connection from the first block to the end of every unit.** Each wait between two
  blocks then holds it, and only a wait through `waits.waiting` gives it back. A lint that bans
  each other kind of wait, such as `asyncio.Event` or `asyncio.sleep`, is a list that a project
  must keep complete. A connection for each transaction, as in SQLAlchemy's `Session`, Rails 7.2
  and Go's `database/sql`, needs no list.
- **A reset that does nothing.** asyncpg's `create_pool(reset=...)` can skip `RESET ALL`. A
  session `SET`, from `connection()` or from a project's own block, then reaches the next unit
  that takes the connection: another tenant, or another actor. The reset costs one round trip.
- **A larger pool.** The bound `Tn × (Cm − 1) + 1` grows with the requests that run at once, so a
  burst passes any size.

## Consequences

- A `GET /tasks` makes 9 round trips where it made 10: `BEGIN`, `SET_TENANT_AND_ACTING`, the page
  and `COMMIT` for the snapshot; `BEGIN`, `SET_TENANT_AND_ACTING`, the insert and `COMMIT` for the
  request event; and one reset. A session check that reads saves its reset in a GET.
- A unit other than a GET or HEAD request holds a connection only while a block runs. Each block
  costs one reset. A `POST /tasks` takes a connection for the write and one for the request event,
  and makes 10 round trips.
- A GET or HEAD request holds its connection from its first block until its response starts or
  it calls `units.return_the_connection`. Another wait between its blocks, such as on an
  `asyncio.Event`, holds it, and nothing bounds that hold before the response starts.
- A wait inside a block that does not go through `waits.waiting` holds the connection, and no
  check sees it. `connection held` names it, and `idle_in_transaction_session_timeout` ends an
  open transaction that waits too long.
- The blocks of the items of an `asyncio.TaskGroup` run one at a time, where their writes ran at
  once on several connections. Outside a GET or HEAD request, each block takes a connection of
  its own.
- Each renewal of a lease is a unit of work of its own, so a job can take a second connection for
  a moment while a block of its attempt holds one. The attempt never waits for the renewal, so the
  two cannot lock the pool.
- A session setting on the connection lasts until the connection goes back: the actor that
  `connection()` names, a `SET` of the project's, and a session advisory lock. Outside a GET or
  HEAD request, that is the end of the block. The pool's reset clears it then.
- A block that took a second connection inside a block now raises `BlockInsideABlock`. A project
  runs `UNIT_OF_WORK_MODE=evaluate` until `unit of work would refuse` names no line.
- Two reads with a wait between them read two snapshots, which may see different data.
- `ReadInsideARead` is `BlockInsideABlock`. `PostgresDatabase.written` and `PostgresDatabase.pool`
  are gone: a write ends the snapshot, and only `Pool` takes a connection.
- The test of the one door does not see a client that a function receives as an argument, a
  client that another module of the project makes, or a client that a `def` calls when the `def`
  goes to `asyncio.to_thread` by name. A review finds it, and `connection held` names a unit that
  holds a connection across it.
- Outside every unit, as in the lifespan, a loop or a test, each block takes a connection of its
  own, as before.
- A unit holds at most one connection of each database it uses. Outside a GET or HEAD request,
  `db.client.connection.use_time` measures one block.
- A reset that fails when a block gives its connection back raises from that block, after its
  transaction committed.
- A later registry of routes can take over `route`, `HOLDS` and whether a route streams: a stream
  gives its connection back when its response starts.
