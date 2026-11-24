"""Every middleware in `app/` is plain ASGI: none runs the app in an anyio task group."""

import ast

from tests.source import APP, modules

ANYIO_MIDDLEWARE = "BaseHTTPMiddleware"


def anyio_middleware(tree: ast.AST) -> list[int]:
    """The line of each `.middleware(...)` call and each name of `BaseHTTPMiddleware` in
    `tree`."""
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "middleware"
        )
        or (isinstance(node, ast.Name) and node.id == ANYIO_MIDDLEWARE)
        or (isinstance(node, ast.Attribute) and node.attr == ANYIO_MIDDLEWARE)
        or (
            isinstance(node, ast.ImportFrom)
            and any(alias.name == ANYIO_MIDDLEWARE for alias in node.names)
        )
    )


def test_no_middleware_in_app_runs_the_app_in_an_anyio_task_group() -> None:
    found = [
        f"{module.path.relative_to(APP.parent)}:{line}"
        for module in modules()
        for line in anyio_middleware(module.tree)
    ]

    assert found == [], (
        f"{found} build a BaseHTTPMiddleware. It runs the app in an anyio task group, and a "
        f"cancel then interrupts the cleanup below it, the end of a snapshot included. Write a "
        f"class with __call__(scope, receive, send) and add it with app.add_middleware."
    )


def test_each_spelling_of_an_anyio_middleware_is_found() -> None:
    source = (
        "from starlette.middleware.base import BaseHTTPMiddleware\n"
        "app.middleware('http')(dispatch)\n"
        "app.add_middleware(BaseHTTPMiddleware, dispatch=dispatch)\n"
        "app.add_middleware(base.BaseHTTPMiddleware, dispatch=dispatch)\n"
        "app.add_middleware(Plain)\n"
    )

    assert anyio_middleware(ast.parse(source)) == [1, 2, 3, 4]
