"""No two decision records share a number, and each heading names its file's number. A gap
passes. `docs/adr/README.md` states the rule."""

from pathlib import Path
from typing import Final

import pytest

DECISIONS: Final = next(
    up / "docs" / "adr" for up in Path(__file__).resolve().parents if (up / "docs" / "adr").is_dir()
)
"""The project's records. The template's are in `template/` below it, numbered apart."""

SETS: Final = (DECISIONS, DECISIONS / "template")


def records(directory: Path) -> list[Path]:
    """Each record in `directory`, in name order."""
    return sorted(directory.glob("[0-9][0-9][0-9][0-9]-*.md"))


def taken_twice(names: list[str]) -> dict[str, list[str]]:
    """Each number that more than one of `names` starts with, with those names."""
    held: dict[str, list[str]] = {}
    for name in names:
        held.setdefault(name[:4], []).append(name)
    return {number: taking for number, taking in held.items() if len(taking) > 1}


def test_the_records_are_where_this_test_reads_them() -> None:
    assert (DECISIONS / "README.md").is_file(), f"{DECISIONS} holds no README.md"
    assert records(DECISIONS / "template"), f"{DECISIONS / 'template'} holds no record"


def test_a_number_two_records_take_is_found() -> None:
    assert taken_twice(["0007-one", "0007-two", "0009-three"]) == {"0007": ["0007-one", "0007-two"]}


@pytest.mark.parametrize("directory", SETS, ids=("project", "template"))
def test_no_two_decision_records_share_a_number(directory: Path) -> None:
    twice = taken_twice([record.name for record in records(directory)])
    assert twice == {}, (
        f"{twice}: two records take one number. The record that merged later takes the number "
        f"after the highest, and every citation of it changes in the same change "
        f"(docs/adr/README.md)."
    )


@pytest.mark.parametrize("directory", SETS, ids=("project", "template"))
def test_each_decision_record_heading_names_its_number(directory: Path) -> None:
    wrong = [
        record.name
        for record in records(directory)
        if not record.read_text().startswith(f"# {record.name[:4]}. ")
    ]
    assert wrong == [], f"{wrong}: the heading does not start with '# NNNN. ' of the file's number."
