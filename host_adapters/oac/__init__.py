"""OAC host adapter package.

The adapter owns legacy wire contracts and host policy. It may call OIR public
application ports, while the generic ``app`` package must remain independent of
this package.
"""

__all__ = [
    "api",
    "fallback",
    "identity",
    "mappers",
    "repositories",
    "schemas",
    "shadow",
]
