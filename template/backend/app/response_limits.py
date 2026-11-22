"""The response policy a route declares before it starts its response."""

from dataclasses import dataclass, field
from typing import Final

from fastapi import Request
from starlette.types import Scope

DEFAULT_BYTES: Final = 4 * 1024 * 1024
CEILING_BYTES: Final = 16 * 1024 * 1024
MINIMUM_BYTES: Final = 1024
STATE: Final = "app.response_limit"


class ResponsePolicyInvalid(ValueError):
    """A response policy is missing, late, or outside the deployment's bounds."""


@dataclass(frozen=True)
class Limits:
    """`default` is the size a response is expected to stay under. A response over its limit is
    sent, and reported once for each route. `ceiling` is the size no response passes: the guard
    refuses a response past it. Set `ceiling` below the largest response the edge in front of the
    app carries. `docs/adr/template/0028`."""

    default: int = DEFAULT_BYTES
    ceiling: int = CEILING_BYTES
    reported: set[str | None] = field(default_factory=set, compare=False, repr=False)
    """Each route whose response was over its limit in this process. Only the guard writes it."""

    def __post_init__(self) -> None:
        if (
            type(self.default) is not int
            or type(self.ceiling) is not int
            or not MINIMUM_BYTES <= self.default <= self.ceiling
        ):
            raise ResponsePolicyInvalid(
                f"Response limits require {MINIMUM_BYTES} <= default <= ceiling"
            )


@dataclass
class Declaration:
    limits: Limits
    maximum: int
    reason: str = "default"
    streaming: bool = False
    started: bool = False
    over_limit: bool = False
    """Whether the response was over `maximum`, and was sent."""


def declare(scope: Scope, limits: Limits) -> Declaration:
    """Install one request's policy. Nested guards share the outer deployment policy."""
    existing = scope.get(STATE)
    if isinstance(existing, Declaration):
        return existing
    declaration = Declaration(limits, limits.default)
    scope[STATE] = declaration
    return declaration


def _before_start(request: Request, reason: str) -> Declaration:
    declaration = request.scope.get(STATE)
    if not isinstance(declaration, Declaration):
        raise ResponsePolicyInvalid("This request has no response guard")
    if declaration.started:
        raise ResponsePolicyInvalid("A response policy must precede response start")
    if not reason.strip():
        raise ResponsePolicyInvalid("A response exception needs a reason")
    return declaration


def allow(request: Request, *, max_bytes: int, reason: str) -> None:
    """Set this response's finite byte limit. Raises outside the deployment's ceiling."""
    declaration = _before_start(request, reason)
    if type(max_bytes) is not int or not MINIMUM_BYTES <= max_bytes <= declaration.limits.ceiling:
        raise ResponsePolicyInvalid(
            f"Response limit {max_bytes!r} must be an integer from "
            f"{MINIMUM_BYTES} to {declaration.limits.ceiling}"
        )
    declaration.streaming = False
    declaration.maximum = max_bytes
    declaration.reason = reason


def ceiling_of(request: Request) -> int:
    """The largest finite limit this request may declare: the deployment's ceiling."""
    declaration = request.scope.get(STATE)
    if not isinstance(declaration, Declaration):
        raise ResponsePolicyInvalid("This request has no response guard")
    return declaration.limits.ceiling


def stream(request: Request, *, reason: str) -> None:
    """Declare incremental delivery without a total byte limit.

    The route owns event or download bounds and stream lifetime. An actor's stream also calls
    `admission.stream`. A failure after delivery starts terminates the stream.
    """
    declaration = _before_start(request, reason)
    declaration.streaming = True
    declaration.reason = reason
