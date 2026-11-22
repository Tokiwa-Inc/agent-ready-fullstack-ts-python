"""No test under a `slow/` folder calls `rglob` or imports `tests.source`. A test that walks
`app/` stays in the commit gate and reads the tree through `tests.source`."""

import ast
from pathlib import Path
from typing import Final

TESTS: Final = Path(__file__).resolve().parent


def _imports_the_shared_parse(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name == "tests.source" for alias in node.names)
    named = node.module.split(".") if node.module else []
    module = named if node.level == 0 else ["tests", *named]
    return module == ["tests", "source"] or (
        module == ["tests"] and any(alias.name == "source" for alias in node.names)
    )


def walks_a_tree(source: str) -> list[int]:
    """The line of each `rglob` call and each import of `tests.source` in `source`."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "rglob"
        )
        or (isinstance(node, ast.Import | ast.ImportFrom) and _imports_the_shared_parse(node))
    ]


def test_no_test_under_a_slow_folder_walks_a_source_tree() -> None:
    found = [
        f"tests/{path.relative_to(TESTS)}:{line}"
        for path in sorted(TESTS.rglob("*.py"))
        if "slow" in path.relative_to(TESTS).parts
        for line in walks_a_tree(path.read_text(encoding="utf-8"))
    ]

    assert found == [], (
        f"{found} walk a source tree under a slow/ folder, which only the full suite runs, not "
        "the commit gate. Move each beside its subject, out of slow/, and read app/ through "
        "tests.source, which parses it once per process."
    )


def test_a_walk_of_a_tree_is_found() -> None:
    assert walks_a_tree("for path in APP.rglob('*.py'):\n    pass\n") == [1]


def test_a_read_of_the_shared_parse_is_found() -> None:
    assert walks_a_tree("import os\nfrom tests.source import nodes\nimport tests.source\n") == [
        2,
        3,
    ]


def test_an_import_of_the_shared_parse_from_its_package_is_found() -> None:
    assert walks_a_tree("from tests import clock, source\nfrom tests import clock\n") == [1]


def test_a_relative_import_of_the_shared_parse_is_found() -> None:
    assert walks_a_tree(
        "from ..source import nodes\nfrom ... import source\nfrom . import x\n"
    ) == [
        1,
        2,
    ]
