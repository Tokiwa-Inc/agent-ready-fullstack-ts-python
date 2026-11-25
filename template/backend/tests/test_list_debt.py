"""Each list of what the project has not fixed yet only shrinks. `backend/.list-debt.json` holds
what each list named when the project recorded it: the entries of a list, or the count of each
entry of a counted list. A list may name no entry that the list debt does not hold, and count no
entry above it.

The template owns this module. The lists are the project's: `app/failures.py` and
`tests/integration/filled.py`. `docs/adr/template/0015`.
"""

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from app.failures import STILL_FREE_TEXT, STILL_SAYS_A_CAUGHT_ERROR
from tests.integration.filled import (
    GROWS_WITH_ROWS,
    NOT_FILLED_YET,
    NOT_READ_YET,
    OVER_A_PAGE,
    REPEATS_A_STATEMENT,
    UNBOUNDED_ANSWERS,
)

LIST_DEBT: Final = ".list-debt.json"

type Counts = Mapping[str, Mapping[str, int]]

NAMED: Final[Mapping[str, Collection[str]]] = {
    "STILL_FREE_TEXT": STILL_FREE_TEXT,
    "OVER_A_PAGE": OVER_A_PAGE,
    "GROWS_WITH_ROWS": GROWS_WITH_ROWS,
    "REPEATS_A_STATEMENT": REPEATS_A_STATEMENT,
    "NOT_FILLED_YET": NOT_FILLED_YET,
    "NOT_READ_YET": NOT_READ_YET,
    "UNBOUNDED_ANSWERS": UNBOUNDED_ANSWERS,
}
"""Each list that names its entries. The list debt holds each as a list of entries."""

COUNTED: Final[Counts] = {
    "STILL_SAYS_A_CAUGHT_ERROR": STILL_SAYS_A_CAUGHT_ERROR,
}
"""Each list that counts each of its entries. The list debt holds each as an object of counts."""


@dataclass(frozen=True)
class Disagreement:
    """Each way that the lists and the list debt disagree."""

    unrecorded: list[str]
    raised: Counts
    lowered: Counts


def counts_of(named: Mapping[str, Collection[str]], counted: Counts) -> Counts:
    """Each list as the count of each of its entries. A named entry counts one."""
    return {
        **{name: dict.fromkeys(entries, 1) for name, entries in named.items()},
        **{name: dict(entries) for name, entries in counted.items()},
    }


def recorded(name: str, value: object, counted: bool) -> dict[str, int]:
    """The counts that the list debt holds for the list `name`: a list of entries, or an object
    of a count above zero for each entry when the list is `counted`. Fails on any other value."""
    if not counted and isinstance(value, list) and all(isinstance(entry, str) for entry in value):
        return dict.fromkeys(value, 1)
    if (
        counted
        and isinstance(value, dict)
        and all(
            isinstance(entry, str) and type(count) is int and count > 0
            for entry, count in value.items()
        )
    ):
        return dict(value)
    shape = "an object of a count above zero for each entry" if counted else "a list of entries"
    pytest.fail(f"{LIST_DEBT} holds {value!r} for {name}. Hold {shape}.")


def beyond(these: Counts, those: Counts) -> Counts:
    """Each entry of each list in both that counts more in `these` than in `those`."""
    found = {
        name: {
            entry: count for entry, count in entries.items() if count > those[name].get(entry, 0)
        }
        for name, entries in these.items()
        if name in those
    }
    return {name: entries for name, entries in found.items() if entries}


def disagreement(lists: Counts, debt: Counts) -> Disagreement:
    """Each list that `debt` does not hold, each entry of `lists` above `debt`, and each entry of
    `debt` above `lists`."""
    return Disagreement(
        unrecorded=sorted(set(lists) - set(debt)),
        raised=beyond(lists, debt),
        lowered=beyond(debt, lists),
    )


def as_debt(lists: Counts, names: Collection[str], counted: Collection[str]) -> str:
    """What the list debt holds for each list in `names`, as JSON."""
    return json.dumps(
        {
            name: dict(sorted(lists[name].items())) if name in counted else sorted(lists[name])
            for name in sorted(names)
        },
        indent=2,
    )


@pytest.fixture
def list_debt(pytestconfig: pytest.Config) -> Path:
    return pytestconfig.rootpath / LIST_DEBT


def test_no_list_names_more_than_the_list_debt_holds(list_debt: Path) -> None:
    lists = counts_of(NAMED, COUNTED)
    assert list_debt.exists(), (
        f"backend/{LIST_DEBT} is missing. Shrink each list as far as you can first: the first "
        f"record is the only one. Then write it as:\n{as_debt(lists, lists, COUNTED)}"
    )
    held: object = json.loads(list_debt.read_text())
    assert isinstance(held, dict), f"backend/{LIST_DEBT} holds {held!r}. Hold an object."
    unknown = sorted(set(held) - set(lists))
    assert unknown == [], f"backend/{LIST_DEBT} holds {unknown}, which is no list. Remove each."
    found = disagreement(
        lists,
        {name: recorded(name, value, name in COUNTED) for name, value in held.items()},
    )

    assert found.unrecorded == [], (
        f"backend/{LIST_DEBT} holds nothing for {found.unrecorded}. Shrink each as far as you can "
        f"first: the first record of a list is the only one. Then add:\n"
        f"{as_debt(lists, found.unrecorded, COUNTED)}"
    )
    assert found.raised == {}, (
        f"Each of these lists names more than backend/{LIST_DEBT} holds: {found.raised}. A list "
        f"only shrinks. Remove each entry, or lower its count, and fix what it named: the test "
        f"that reads the list says how. Do not raise the list debt."
    )
    assert found.lowered == {}, (
        f"backend/{LIST_DEBT} holds more than these lists name: {found.lowered}. Lower it, so "
        f"that no entry comes back. Write each as:\n{as_debt(lists, found.lowered, COUNTED)}"
    )


def test_an_entry_beyond_the_list_debt_and_a_debt_beyond_its_list_are_each_named() -> None:
    lists = counts_of({"A": ["/a", "/b"], "B": []}, {"C": {"app/c.py": 3}})
    debt = {"A": {"/a": 1, "/b": 1}, "B": {}, "C": {"app/c.py": 3}}

    assert disagreement(lists, debt) == Disagreement([], {}, {})
    assert disagreement(lists, {**debt, "A": {"/a": 1}}).raised == {"A": {"/b": 1}}
    assert disagreement(lists, {**debt, "C": {"app/c.py": 2}}).raised == {"C": {"app/c.py": 3}}
    assert disagreement(lists, {**debt, "B": {"/z": 1}}).lowered == {"B": {"/z": 1}}
    assert disagreement(lists, {**debt, "C": {"app/c.py": 4}}).lowered == {"C": {"app/c.py": 4}}
    assert disagreement(lists, {"A": debt["A"]}) == Disagreement(["B", "C"], {}, {})


def test_the_list_debt_reads_each_list_in_its_own_shape_and_fails_on_any_other() -> None:
    assert recorded("A", ["/a"], counted=False) == {"/a": 1}
    assert recorded("C", {"app/c.py": 2}, counted=True) == {"app/c.py": 2}
    for value in ("/a", [1], {"/a": 1}, None):
        with pytest.raises(pytest.fail.Exception):
            _ = recorded("A", value, counted=False)
    for value in (["app/c.py"], {"app/c.py": 0}, {"app/c.py": "2"}, {"app/c.py": True}):
        with pytest.raises(pytest.fail.Exception):
            _ = recorded("C", value, counted=True)


def test_what_the_test_says_to_write_is_what_the_list_debt_reads_back() -> None:
    lists = counts_of({"A": ["/b", "/a"], "B": []}, {"C": {"app/c.py": 3}})
    written: dict[str, object] = json.loads(as_debt(lists, lists, {"C"}))

    assert written == {"A": ["/a", "/b"], "B": [], "C": {"app/c.py": 3}}
    assert {name: recorded(name, value, name == "C") for name, value in written.items()} == lists
