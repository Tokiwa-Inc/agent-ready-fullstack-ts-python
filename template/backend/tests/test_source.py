"""A test that walks a source tree is a rule every change keeps, so the commit gate runs it. It
reads `app/` through `tests.source`, which parses the tree once per process, and it never lives
under a `slow/` folder, which only the suite runs, after the change merged."""

import ast
from pathlib import Path
from typing import Final

TESTS: Final = Path(__file__).resolve().parent


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
        or (isinstance(node, ast.ImportFrom) and node.module == "tests.source")
        or (isinstance(node, ast.Import) and any(a.name == "tests.source" for a in node.names))
    ]


def test_no_test_under_a_slow_folder_walks_a_source_tree() -> None:
    found = [
        f"tests/{path.relative_to(TESTS)}:{line}"
        for path in sorted(TESTS.rglob("*.py"))
        if "slow" in path.relative_to(TESTS).parts
        for line in walks_a_tree(path.read_text(encoding="utf-8"))
    ]

    assert found == [], (
        f"{found} walk a source tree under a slow/ folder, which only the suite runs. Move each "
        "beside its subject, out of slow/, and read app/ through tests.source, which parses it "
        "once per process."
    )


def test_a_walk_of_a_tree_is_found() -> None:
    assert walks_a_tree("for path in APP.rglob('*.py'):\n    pass\n") == [1]


def test_a_read_of_the_shared_parse_is_found() -> None:
    assert walks_a_tree("import os\nfrom tests.source import nodes\nimport tests.source\n") == [
        2,
        3,
    ]
