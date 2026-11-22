"""The Python source of `backend/app/`, read and parsed once per process, for the rules that walk
every module of it. A rule that reads the tree here stays in the commit gate: `tests/test_source.py`
refuses one under a `slow/` folder."""

import ast
from functools import cache
from pathlib import Path
from typing import Final, NamedTuple

APP: Final = Path(__file__).resolve().parents[1] / "app"
"""`backend/app/`."""


class Module(NamedTuple):
    """One module of the source: its path, its text, and its syntax tree. A rule reads the tree
    and does not change it, because every other rule in the process reads the same one."""

    path: Path
    text: str
    tree: ast.Module


@cache
def modules() -> tuple[Module, ...]:
    """Every module under `APP`, in path order."""
    read = [(path, path.read_text(encoding="utf-8")) for path in sorted(APP.rglob("*.py"))]
    return tuple(Module(path, text, ast.parse(text)) for path, text in read)


@cache
def nodes() -> tuple[tuple[Module, ast.AST], ...]:
    """Every node of every module under `APP`, with its module, in `ast.walk` order."""
    return tuple((module, node) for module in modules() for node in ast.walk(module.tree))
