"""Ok/Err result objects for expected failures (CLAUDE.md style: service functions
return a result object rather than raising for a failure the caller is meant to
handle, e.g. a fix that fails validation)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Ok[T]:
    value: T


@dataclass(frozen=True)
class Err:
    reason: str


type Result[T] = Ok[T] | Err
