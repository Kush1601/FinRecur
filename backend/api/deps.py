"""Simulated role switch (spec 3.6, 3.7). `finrecur_role`/`finrecur_name` are a
cookie the web client sets from its own role switcher UI; this is NOT auth -- any
client can set these cookies to claim any role or name. The approve/reject/apply
routes take role and name explicitly in the request body (matching the frontend
contract), so this dependency is for routes that only need to know who's asking
without a body, e.g. defaulting a display name."""

from dataclasses import dataclass

from fastapi import Cookie

VALID_ROLES = ("reviewer", "approver")


@dataclass(frozen=True)
class SimulatedActor:
    role: str
    name: str


def get_actor(
    finrecur_role: str = Cookie(default="reviewer"),
    finrecur_name: str = Cookie(default="Reviewer"),
) -> SimulatedActor:
    role = finrecur_role if finrecur_role in VALID_ROLES else "reviewer"
    return SimulatedActor(role=role, name=finrecur_name)
