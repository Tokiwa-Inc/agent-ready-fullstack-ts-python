"""What a snapshot keeps: each answer, so that the same statement with the same arguments reaches
the database once in it. `docs/adr/template/0033`.

A snapshot is one transaction, READ ONLY and REPEATABLE READ, on the connection of a unit of work
that reads through one: a GET or HEAD request. `app/store/units.py` opens it at the first read of
a tenant, points it at each tenant that a read names, and ends it before a block that does not
read the snapshot, when a read in it fails in the database, and when the unit gives its
connection back. The next read then opens a new one, which keeps nothing of the old one.
"""

import re
from collections.abc import Hashable
from typing import Any, Final

SENT_EACH_TIME: Final = re.compile(
    r"\b(clock_timestamp|statement_timestamp|timeofday|random|random_normal|gen_random_uuid|"
    r"uuidv[47]|uuid_generate_v\w+|pg_(try_)?advisory_\w+)\s*\(",
    re.IGNORECASE,
)
"""A statement that calls a function whose answer changes inside one transaction. A snapshot
sends it each time, and keeps nothing of its answer."""


class Kept:
    """A connection in a snapshot, for one tenant, that answers a read it already answered from
    what it kept.

    A value that a read answers is the same object each time: change none of it."""

    def __init__(self, conn: Any) -> None:
        self._conn: Any = conn
        self._kept: Final[dict[Hashable, Any]] = {}

    async def fetch(self, query: str, *args: Any, **kwargs: Any) -> list[Any]:
        rows: list[Any] = await self._answered("fetch", query, args, kwargs)
        return list(rows)

    async def fetchrow(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._answered("fetchrow", query, args, kwargs)

    async def fetchval(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._answered("fetchval", query, args, kwargs)

    async def _answered(
        self, method: str, query: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        sent = getattr(self._conn, method)
        if SENT_EACH_TIME.search(query) is not None:
            return await sent(query, *args, **kwargs)
        asked = (method, query, _frozen(args), _frozen(kwargs))
        try:
            hash(asked)
        except TypeError as unhashable:
            raise TypeError(
                f"a read in a snapshot passed an argument that cannot be kept: {unhashable}. "
                f"Pass a str, a number, a UUID, or a list, tuple, set or dict of them."
            ) from unhashable
        if asked not in self._kept:
            self._kept[asked] = await sent(query, *args, **kwargs)
        return self._kept[asked]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _frozen(value: Any) -> Any:
    if isinstance(value, list | tuple):
        return (type(value).__name__, tuple(_frozen(item) for item in value))
    if isinstance(value, set | frozenset):
        return (type(value).__name__, frozenset(_frozen(item) for item in value))
    if isinstance(value, dict):
        return ("dict", frozenset((key, _frozen(item)) for key, item in value.items()))
    return (type(value).__name__, value)
