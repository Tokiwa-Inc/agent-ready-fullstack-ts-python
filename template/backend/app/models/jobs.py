from typing import Literal

from pydantic import BaseModel, JsonValue

from app.models.failures import Failure
from app.models.shared import Id, Name

JsonObject = dict[str, JsonValue]
"""The input of a job, and the outcome of a job that is done."""

JobState = Literal["pending", "done", "dead"]
"""`pending` until a worker ends the job. `done` with an outcome, or `dead` with a failure."""


class Job(BaseModel):
    """One job, as the tenant that asked for it reads it. `outcome` is set when the job is `done`,
    and `failure` says why it is `dead`."""

    id: Id
    kind: Name
    state: JobState
    outcome: JsonObject | None
    failure: Failure | None
