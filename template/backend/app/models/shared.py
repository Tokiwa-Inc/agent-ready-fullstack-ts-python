"""What every other module here may build on, and which builds on nothing."""

from typing import Annotated, Any, Final

from pydantic import BaseModel, Field

PAGE_MAX: Final = 100
"""The most rows one page of a list holds. `docs/adr/template/0026`."""

PAGE_BYTES: Final = 1024 * 1024
"""The most bytes one page of a list holds, as Postgres measures its rows. A page always holds its
first row. `docs/adr/template/0026`."""

ID_MAX: Final = 36
"""The most characters an id holds: a uuid as text."""

NAME_MAX: Final = 1024
"""The most characters a name holds, such as the kind of a job."""


def at_most(characters: int) -> Any:
    """The bound of a string in an answer, for `Annotated[str, at_most(n)]`: at most `characters`
    characters. The schema says so, and nothing checks it when the answer is built, so a value
    stored before the bound still reads. The response guard reports an answer over its limit.
    `docs/adr/template/0028`."""
    return Field(json_schema_extra={"maxLength": characters})


Id = Annotated[str, at_most(ID_MAX)]
"""An id in an answer."""

Name = Annotated[str, at_most(NAME_MAX)]
"""A name in an answer, such as the kind of a job."""


class ErrorBody(BaseModel):
    detail: str
