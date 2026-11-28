"""A wait on a system outside the process runs inside `app.waits.waiting`: the one door to it,
which gives back the connection that a GET or HEAD request holds first. `docs/adr/template/0034`.

The test reads each module of `app/`. A client is a name that the module imports from a module of
`OUTSIDE`, a name that a scope binds to one or to what one makes, or an attribute of `self` that a
class binds to one. The test fails on each `await`, each `async for` and each `async with` of a
client, in an `async def` and not inside an `async with` of `waiting`, whose expression holds a
client or a function of `OUTSIDE_IN_ASYNCIO` anywhere in it: in an argument, a lambda or a call
that it hands to a thread. The template owns this module.
"""

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Final, override

import pytest

APP: Final = Path(__file__).resolve().parents[2] / "app"

OUTSIDE: Final = (
    "aioboto3",
    "aiobotocore",
    "aiohttp",
    "aiosmtplib",
    "anthropic",
    "boto3",
    "botocore",
    "ftplib",
    "google.cloud",
    "googleapiclient",
    "grpc",
    "http.client",
    "httpx",
    "openai",
    "redis",
    "requests",
    "smtplib",
    "socket",
    "subprocess",
    "urllib.request",
    "websockets",
)
"""The modules whose calls reach a system outside the process."""

OUTSIDE_IN_ASYNCIO: Final = frozenset(
    {"open_connection", "open_unix_connection", "create_subprocess_exec", "create_subprocess_shell"}
)
"""The functions of `asyncio` that reach a system outside the process."""


def _outside(module: str) -> bool:
    return any(module == one or module.startswith(f"{one}.") for one in OUTSIDE)


def _root(node: ast.expr) -> ast.expr:
    while isinstance(node, ast.Attribute | ast.Call | ast.Subscript | ast.Await):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node


def _self_attribute(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute | ast.Call | ast.Subscript | ast.Await):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            return node.attr
        node = node.func if isinstance(node, ast.Call) else node.value
    return None


def _named(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else None


def _waiting(item: ast.withitem) -> bool:
    called = item.context_expr
    return isinstance(called, ast.Call) and _named(called.func) == "waiting"


def _imported(tree: ast.Module) -> set[str]:
    """Each name that `tree` imports from a module of `OUTSIDE`."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(
                alias.asname or alias.name.split(".")[0]
                for alias in node.names
                if _outside(alias.name)
            )
        if isinstance(node, ast.ImportFrom) and node.module is not None and _outside(node.module):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


class _Doors(ast.NodeVisitor):
    """Each wait on a client in an `async def` that is not inside an `async with` of `waiting`.

    A name is a client in the scope that binds it, and in each scope inside that one. An
    attribute of `self` is a client in the class that binds it."""

    def __init__(self, imported: set[str]) -> None:
        self._imported: Final = imported
        self._scopes: list[set[str]] = [set(imported)]
        self._classes: list[set[str]] = []
        self._asynchronous: list[bool] = []
        self._waits: int = 0
        self.found: list[int] = []

    def _client(self, node: ast.expr) -> bool:
        root = _root(node)
        if isinstance(root, ast.Name) and any(root.id in scope for scope in self._scopes):
            return True
        attribute = _self_attribute(node)
        return bool(self._classes) and attribute in self._classes[-1]

    def _instance(self, node: ast.expr) -> bool:
        root = _root(node)
        return self._client(node) and not (isinstance(root, ast.Name) and root.id in self._imported)

    def _waits_outside(self, node: ast.expr) -> bool:
        return any(
            isinstance(inner, ast.expr)
            and (
                self._client(inner)
                or (isinstance(inner, ast.Call) and _named(inner.func) in OUTSIDE_IN_ASYNCIO)
            )
            for inner in ast.walk(node)
        )

    def _check(self, node: ast.expr, line: int) -> None:
        if (
            self._asynchronous
            and self._asynchronous[-1]
            and self._waits == 0
            and self._waits_outside(node)
        ):
            self.found.append(line)

    def _bind(self, target: ast.expr, value: ast.expr, *, entered: bool) -> None:
        if isinstance(value, ast.Await) or not self._client(value):
            return
        if not entered and isinstance(value, ast.Call) and self._instance(value):
            return
        if isinstance(target, ast.Name):
            self._scopes[-1].add(target.id)
        attribute = _self_attribute(target)
        if attribute is not None and self._classes:
            self._classes[-1].add(attribute)

    @override
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._classes.append(
            {
                attribute
                for statement in ast.walk(node)
                if isinstance(statement, ast.Assign | ast.AnnAssign)
                and statement.value is not None
                and not isinstance(statement.value, ast.Await)
                and self._client(statement.value)
                for target in (
                    statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                )
                if (attribute := _self_attribute(target)) is not None
            }
        )
        self._within(node, asynchronous=False)
        _ = self._classes.pop()

    @override
    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._within(node, asynchronous=True)

    @override
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._within(node, asynchronous=False)

    @override
    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._within(node, asynchronous=False)

    def _within(self, node: ast.AST, *, asynchronous: bool) -> None:
        waits, self._waits = self._waits, 0
        self._asynchronous.append(asynchronous)
        self._scopes.append(set())
        self.generic_visit(node)
        _ = self._scopes.pop()
        _ = self._asynchronous.pop()
        self._waits = waits

    @override
    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._bind(target, node.value, entered=False)

    @override
    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            self._bind(node.target, node.value, entered=False)

    @override
    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._bind(item.optional_vars, item.context_expr, entered=True)
        for statement in node.body:
            self.visit(statement)

    @override
    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        waited = 0
        for item in node.items:
            if self._instance(item.context_expr):
                self._check(item.context_expr, item.context_expr.lineno)
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._bind(item.optional_vars, item.context_expr, entered=True)
            if _waiting(item):
                waited += 1
                self._waits += 1
        for statement in node.body:
            self.visit(statement)
        self._waits -= waited

    @override
    def visit_Await(self, node: ast.Await) -> None:
        self._check(node.value, node.lineno)
        self.generic_visit(node)

    @override
    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._check(node.iter, node.lineno)
        self.generic_visit(node)


def outside_the_door(source: str) -> list[int]:
    """The line of each wait on a client in `source` that is not inside `waiting`."""
    tree = ast.parse(source)
    doors = _Doors(_imported(tree))
    doors.visit(tree)
    return sorted(set(doors.found))


def modules() -> Iterator[Path]:
    yield from sorted(APP.rglob("*.py"))


def test_each_wait_on_a_system_outside_the_process_goes_through_waiting() -> None:
    found = {
        str(module.relative_to(APP.parent)): lines
        for module in modules()
        if (lines := outside_the_door(module.read_text()))
    }
    assert found == {}, (
        f"Each of these lines waits on a system outside the process outside app.waits.waiting: "
        f"{found}. Wait inside `async with waiting(seconds, on=...)`, which gives the connection "
        f"of the unit of work back before the wait."
    )


@pytest.mark.parametrize(
    "source",
    [
        "import httpx\nasync def f():\n    async with httpx.AsyncClient() as c:\n"
        "        await c.get('x')\n",
        "from httpx import AsyncClient\nasync def f():\n    await AsyncClient().get('x')\n",
        "import httpx\nCLIENT = httpx.AsyncClient()\nasync def f():\n    await CLIENT.get('x')\n",
        "import asyncio\nimport google.cloud.storage as gcs\nclass S:\n    def __init__(self):\n"
        "        self.c = gcs.Client()\n    async def f(self):\n"
        "        await asyncio.to_thread(self.c.bucket('b').blob('n').download_as_bytes)\n",
        "import asyncio\nasync def f():\n    await asyncio.open_connection('h', 1)\n",
        "import httpx\nasync def f():\n    async with httpx.stream('GET', 'x') as r:\n"
        "        async for line in r.aiter_lines():\n            pass\n",
        "import asyncio, httpx\nC = httpx.AsyncClient()\nasync def f():\n"
        "    await asyncio.gather(C.get('a'), C.get('b'))\n",
        "import asyncio, httpx\nC = httpx.AsyncClient()\nasync def f():\n"
        "    await asyncio.wait_for(C.get('a'), 5)\n",
        "import asyncio, requests\nasync def f():\n    await asyncio.to_thread(lambda: requests.get('x'))\n",
        "import asyncio, functools, requests\nasync def f(loop):\n"
        "    await loop.run_in_executor(None, functools.partial(requests.get, 'x'))\n",
        "import aiohttp\nS = aiohttp.ClientSession()\nasync def f():\n"
        "    async with S.post('x') as r:\n        pass\n",
    ],
)
def test_a_wait_on_a_client_outside_waiting_is_found(source: str) -> None:
    assert outside_the_door(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "import httpx\nfrom app.waits import waiting\nasync def f():\n"
        "    async with waiting(1, on='x'), httpx.AsyncClient() as c:\n        await c.get('x')\n",
        "import httpx\nfrom app import waits\nCLIENT = httpx.AsyncClient()\nasync def f():\n"
        "    async with waits.waiting(1, on='x'):\n        await CLIENT.get('x')\n",
        "import httpx\nfrom app.waits import waiting\nasync def f(c: httpx.AsyncClient):\n"
        "    async with httpx.AsyncClient() as c:\n        try:\n"
        "            async with waiting(1, on='x'):\n                await c.get('x')\n"
        "        except httpx.HTTPError:\n            raise\n",
        "import httpx\nclass S:\n    def __init__(self):\n        self.c = httpx.Client()\n",
        "import asyncio\nasync def f():\n    await asyncio.to_thread(sorted, [])\n",
        "import httpx\nasync def f():\n    c = httpx.AsyncClient()\nasync def g(store):\n"
        "    c = store\n    await c.read()\n",
        "import httpx\nclass A:\n    def __init__(self):\n        self.c = httpx.Client()\n"
        "class B:\n    async def f(self):\n        await self.c.read()\n",
        "import httpx\nfrom app.waits import waiting\nasync def f(c):\n"
        "    async with waiting(1, on='x'):\n        r = await c.get('x')\n"
        "    body = r.json()\n    await save(body)\n",
    ],
)
def test_a_wait_inside_waiting_and_a_client_that_is_not_awaited_are_not_found(
    source: str,
) -> None:
    assert outside_the_door(source) == []
