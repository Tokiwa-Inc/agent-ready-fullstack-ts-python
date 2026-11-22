from typing import Annotated, Final

from pydantic import BaseModel, Field

from app.models.shared import PAGE_MAX, Id, at_most

TITLE_MAX: Final = 500
"""The most characters a task's title holds."""

Title = Annotated[str, at_most(TITLE_MAX)]
"""A task's title in an answer."""


class Task(BaseModel):
    id: Id
    title: Title
    done: bool


class TaskPage(BaseModel):
    tasks: list[Task] = Field(max_length=PAGE_MAX)
    next: Id | None


class CreateTaskBody(BaseModel):
    title: str = Field(max_length=TITLE_MAX)


class UpdateTaskBody(BaseModel):
    done: bool
