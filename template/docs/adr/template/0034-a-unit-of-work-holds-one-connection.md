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

## Decision

- **A unit of work is what one request, one attempt of a job, one run of shared work, or one task
  of work beside the requests does on the database.** `app/store/units.py` holds it. The boundary
  opens it: `UnitPerRequest` for each HTTP request, `run` in `app/worker.py` for each attempt and
  `_renew` for each renewal of its lease, and `_started` in `app/waits.py` for each run and each
  task. A part of `GET /reads` is a request,
  so it is a unit of its own. A turn of a loader runs in the unit of its callers, through
  `SharedWork(..., in_the_callers_unit=True)`.
- **A unit holds at most `HOLDS` connection of each database at a time.** Its first use of the
  database takes a connection from the pool. `lent`, `connection`, `reading` and `scoped` of
  `PostgresDatabase` each run a block on that connection, through `units.block`. `Pool` in
  `app/store/pg.py` is the one place that takes a connection from asyncpg and gives it back, and
  `PostgresDatabase` keeps it private. So no store can take a second connection in a unit.
  `Pool.taken` refuses a unit a second connection of one pool all the same, and
  `test_a_unit_that_asks_one_pool_for_a_second_connection_is_refused` holds it.
- **The blocks of a unit run on its connection one at a time.** The items of an
  `asyncio.TaskGroup` share it. A block inside a block of the same task runs in the transaction of
  the outer block when `Need.shares` allows it: a read or a bare block inside a block of the same
  tenant, and a write inside a write. Any other block inside a block raises `BlockInsideABlock`, and
  so does a block in a task that a block started. Each would wait for ever for the outer block.
  `tests/store/test_units.py` holds each rule.
- **A GET or HEAD request reads one snapshot on its connection.**
  [0033](0033-a-get-request-reads-one-snapshot-and-sends-each-statement-once.md) says what a
  snapshot is and what it keeps. A read of another tenant points the snapshot at that tenant with
  one statement, `SET_TENANT_AND_ACTING`, in the same transaction. The snapshot keeps the answers of
  each tenant apart. A block that does not read the snapshot, such as a write, ends it first.
- **Outside a GET request, each `reading` and each `scoped` block opens a transaction of its own
  on the connection of the unit.** `scoped` opens one that
  [0017](0017-the-database-records-every-write-and-the-request-layer-every-request.md) records,
  and `reading` one that is READ ONLY. A request that writes is not one transaction.
- **The unit gives its connection back when it ends, when its response starts, and before it waits
  on anything but that connection.** `UnitPerRequest` gives it back at `http.response.start`, after
  the request event. A block that is still open then, as in a dependency with `yield`, gives it back
  when it ends. After the start, and after the unit ends, each block takes a connection of its own
  and gives it back when it ends. `units.return_the_connection` is the one step for a wait.
  `waits.waiting` calls it before its bound, `SharedWork.joined` before a caller joins a run of
  another unit, and `reads.answer` before the parts of a group start. The next block takes a
  connection again, and a read after the wait opens a new snapshot. A wait inside a block raises
  `HeldAcrossAWait`, because the block holds the connection until it ends.
- **The session check and the request event use the connection of the unit.** The identity seam
  runs before the tenant is known, so it reads in a bare block. The first read of the snapshot then
  sets the tenant and the actor on the same connection. `RecordsRequests` records the request event
  in a write block, which ends the snapshot first.
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
  would refuse takes a connection of its own, and a wait inside a block keeps the connection.
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
- **Give the connection back between blocks.** Each block then costs the pool's reset, and a
  request takes 3 connections again, one after another.
- **A larger pool.** The bound `Tn × (Cm − 1) + 1` grows with the requests that run at once, so a
  burst passes any size.

## Consequences

- A `GET /tasks` makes 9 round trips where it made 10: `BEGIN`, `SET_TENANT_AND_ACTING`, the page
  and `COMMIT` for the snapshot; `BEGIN`, `SET_TENANT_AND_ACTING`, the insert and `COMMIT` for the
  request event; and one reset. A `POST /tasks` makes 9 where it made 10. Each further connection
  that a request took in turn, such as for a session check, saves its reset.
- A unit holds its connection from its first block until it ends, its response starts or it waits.
  Between blocks the connection is idle, outside a transaction or in the snapshot. Size the pool
  for the units that run at once.
- The blocks of the items of an `asyncio.TaskGroup` run one at a time, where their writes ran at
  once on several connections.
- Each renewal of a lease is a unit of work of its own, so a job takes a second connection for
  a moment while its attempt holds one. The attempt never waits for the renewal, so the two
  cannot lock the pool.
- In a unit, a session setting on the connection lasts until the unit gives it back: the actor
  that `connection()` names, and a `SET` of the project's. The pool's reset clears it then.
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
- A unit holds one connection of each database it uses.
- A later registry of routes can take over `route`, `HOLDS` and whether a route streams: a stream
  gives its connection back when its response starts.
