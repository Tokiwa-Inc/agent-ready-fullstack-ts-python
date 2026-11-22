"""The Python source of `backend/app/`, read and parsed once per process, for the tests that walk
every module of it. `tests/test_source.py` refuses a reader of it under a `slow/` folder."""

import ast
from functools import cache
from pathlib import Path
from typing import Final, NamedTuple

APP: Final = Path(__file__).resolve().parents[1] / "app"
"""`backend/app/`."""


class Module(NamedTuple):
    """One module of the source: its path and its syntax tree. Every reader in the process shares
    the tree, so a reader must not change it."""

    path: Path
    tree: ast.Module


@cache
def modules() -> tuple[Module, ...]:
    """Every module under `APP`, in path order."""
    return tuple(
        Module(path, ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(APP.rglob("*.py"))
    )


@cache
def nodes() -> tuple[tuple[Module, ast.AST], ...]:
    """Every node of every module under `APP`, with its module, in `ast.walk` order."""
    return tuple((module, node) for module in modules() for node in ast.walk(module.tree))
