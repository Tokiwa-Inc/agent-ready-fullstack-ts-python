"""Every successful answer of every route has a largest size, worked out from its schema before the
app runs, and that size fits the default limit of a response. `UNBOUNDED_ANSWERS` in
`tests/integration/filled.py` names each route that does not fit yet. `docs/adr/template/0028`."""

import json
import re
from collections.abc import Iterator
from typing import Any, Final

import pytest

from app.main import create_app
from app.response_limits import DEFAULT_BYTES
from tests.integration.filled import UNBOUNDED_ANSWERS

NUMBER_BYTES: Final = 24
"""The most bytes a JSON number takes: a 64-bit integer, or a float as Python writes one."""

CHARACTER_BYTES: Final = 6
"""The most bytes one character of a JSON string takes: `\\u001f`."""

FIXED_FORMATS: Final = {"uuid": 38, "date-time": 40, "date": 12}
"""The most bytes a string of each format takes, quotes included."""

_SCALARS: Final = {"null": 4, "boolean": 5, "integer": NUMBER_BYTES, "number": NUMBER_BYTES}
"""The most bytes a JSON value of each type that holds nothing else takes."""

Schemas = dict[str, Any]


class Unbounded(Exception):
    """A part of a schema has no largest size. The message names its path in the answer."""

    def __init__(self, where: str, why: str) -> None:
        super().__init__(f"{where} {why}")


def largest(schema: Schemas, schemas: Schemas, where: str, seen: frozenset[str]) -> int:
    """The most bytes a JSON value that `schema` describes takes. `schemas` holds each schema it
    may refer to, and `seen` each one it is already inside. Raises `Unbounded`."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if name in seen:
            raise Unbounded(where, f"refers to {name} inside itself")
        return largest(schemas[name], schemas, where, seen | {name})
    branches = schema.get("anyOf", schema.get("oneOf"))
    if branches is not None:
        return max(largest(branch, schemas, where, seen) for branch in branches)
    listed = [schema["const"]] if "const" in schema else schema.get("enum")
    if listed is not None:
        return max(len(json.dumps(value).encode()) for value in listed)
    kind = schema.get("type")
    if isinstance(kind, list):
        return max(largest({**schema, "type": one}, schemas, where, seen) for one in kind)
    if kind in _SCALARS:
        return _SCALARS[kind]
    if kind not in _HOLDERS:
        raise Unbounded(where, "may hold any JSON value")
    return _HOLDERS[kind](schema, schemas, where, seen)


def _string(schema: Schemas, _schemas: Schemas, where: str, _seen: frozenset[str]) -> int:
    if "maxLength" in schema:
        return 2 + CHARACTER_BYTES * int(schema["maxLength"])
    if schema.get("format") in FIXED_FORMATS:
        return FIXED_FORMATS[schema["format"]]
    raise Unbounded(where, "is a string with no maxLength")


def _array(schema: Schemas, schemas: Schemas, where: str, seen: frozenset[str]) -> int:
    if "maxItems" not in schema:
        raise Unbounded(where, "is an array with no maxItems")
    item = largest(schema.get("items", {}), schemas, f"{where}[]", seen)
    return 2 + int(schema["maxItems"]) * (item + 1)


def _object(schema: Schemas, schemas: Schemas, where: str, seen: frozenset[str]) -> int:
    size = 2 + sum(
        len(json.dumps(name).encode()) + 2 + largest(value, schemas, f"{where}.{name}", seen)
        for name, value in schema.get("properties", {}).items()
    )
    more = schema.get("additionalProperties", False)
    if more is False:
        return size
    if "maxProperties" not in schema:
        raise Unbounded(where, "holds any number of keys, with no maxProperties")
    names = schema.get("propertyNames", {"type": "string"})
    key = _string(names, schemas, f"{where} key", seen)
    value = largest(more if isinstance(more, dict) else {}, schemas, f"{where}.*", seen)
    return size + int(schema["maxProperties"]) * (key + 2 + value)


_HOLDERS: Final = {"string": _string, "array": _array, "object": _object}
"""How to work out the most bytes a value of each type that holds other values takes."""


def over(schema: Schemas, schemas: Schemas) -> str | None:
    """Why an answer of `schema` does not fit the default limit, or `None` when it fits."""
    try:
        size = largest(schema, schemas, "$", frozenset())
    except Unbounded as unbounded:
        return str(unbounded)
    return None if size <= DEFAULT_BYTES else f"answers up to {size} bytes, over {DEFAULT_BYTES}"


def answers() -> Iterator[tuple[str, str | None]]:
    """Each route, as `METHOD /path`, with why one of its successful answers does not fit, or
    `None` when each fits. An answer of each media type counts, so a text or a file answer is
    walked as a JSON one is."""
    spec = create_app().openapi()
    schemas = spec.get("components", {}).get("schemas", {})
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            whys = [
                why
                for status, answer in operation.get("responses", {}).items()
                if status.startswith("2")
                for content in answer.get("content", {}).values()
                if (why := over(content.get("schema", {}), schemas)) is not None
            ]
            yield f"{method.upper()} {path}", next(iter(whys), None)


def test_every_answer_has_a_largest_size_that_fits_the_default_limit() -> None:
    found = [
        f"{route}: {why}"
        for route, why in answers()
        if route not in UNBOUNDED_ANSWERS and why is not None
    ]

    assert found == [], (
        "Bound each part of these answers in its model: at_most on a str, max_length on a "
        "list. Or name the route in UNBOUNDED_ANSWERS with a reason.\n" + "\n".join(found)
    )


def test_every_route_that_unbounded_answers_names_is_still_unbounded() -> None:
    routes = dict(answers())

    assert [route for route in UNBOUNDED_ANSWERS if route not in routes] == []
    assert [route for route in UNBOUNDED_ANSWERS if routes[route] is None] == []
    assert [route for route, reason in UNBOUNDED_ANSWERS.items() if not reason.strip()] == []


SMALL: Final = {"type": "string", "maxLength": 1}


@pytest.mark.parametrize(
    "schema,where",
    [
        ({"type": "array", "items": SMALL}, "$ is an array with no maxItems"),
        ({"type": "string"}, "$ is a string with no maxLength"),
        ({}, "$ may hold any JSON value"),
        ({"$ref": "#/components/schemas/Node"}, "$.child refers to Node inside itself"),
        (
            {"type": "object", "additionalProperties": SMALL},
            "$ holds any number of keys, with no maxProperties",
        ),
        (
            {"type": "object", "properties": {"rows": {"type": "array", "items": {}}}},
            "$.rows is an array with no maxItems",
        ),
    ],
)
def test_an_unbounded_part_is_named_by_its_path(schema: Schemas, where: str) -> None:
    schemas = {
        "Node": {"type": "object", "properties": {"child": {"$ref": "#/components/schemas/Node"}}}
    }

    with pytest.raises(Unbounded, match=f"^{re.escape(where)}$"):
        largest(schema, schemas, "$", frozenset())


def test_a_bounded_answer_is_its_parts_added_up() -> None:
    page = {
        "type": "object",
        "properties": {"rows": {"type": "array", "maxItems": 2, "items": SMALL}, "next": SMALL},
    }

    assert largest(page, {}, "$", frozenset()) == 2 + (6 + 2 + (2 + 2 * (8 + 1))) + (6 + 2 + 8)


@pytest.mark.parametrize(
    "schema,size",
    [
        ({"enum": ["a", "bcd"]}, 5),
        ({"const": "ab"}, 4),
        ({"type": "string", "format": "uuid"}, 38),
        ({"anyOf": [SMALL, {"type": "null"}]}, 8),
        ({"type": ["string", "null"], "maxLength": 1}, 8),
        ({"type": "boolean"}, 5),
    ],
)
def test_a_bounded_part_takes_its_largest_size(schema: Schemas, size: int) -> None:
    assert largest(schema, {}, "$", frozenset()) == size


def test_an_answer_over_the_default_limit_is_reported_with_its_size() -> None:
    wide = {"type": "array", "maxItems": DEFAULT_BYTES, "items": SMALL}

    assert over(wide, {}) == f"answers up to {2 + DEFAULT_BYTES * 9} bytes, over {DEFAULT_BYTES}"
