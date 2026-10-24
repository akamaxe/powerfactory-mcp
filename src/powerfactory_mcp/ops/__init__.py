"""Operations that run inside the worker (PowerFactory's single thread).

Each module registers functions with @op; the worker dispatches them by name.
Signature: fn(ctx, **args) -> dict | list | scalar (converted to JSON).
"""

from __future__ import annotations

from typing import Callable

OPS: dict[str, Callable] = {}


class OpError(Exception):
    """Expected error (invalid data, missing object...): reported without a traceback."""


def op(name: str | None = None):
    def deco(fn):
        OPS[name or fn.__name__] = fn
        return fn
    return deco


def load_all() -> None:
    # Importing registers the operations.
    from powerfactory_mcp.ops import (  # noqa: F401
        session, project, model, pyexec, loadflow, shortcircuit, results, graphics,
        dynamics, protection, studies,
    )
