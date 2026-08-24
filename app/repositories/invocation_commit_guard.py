"""Last-moment database commit guards for absolute Invocation deadlines."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.errors import InvocationDeadlineExceededError

_INVOCATION_COMMIT_GUARD_KEY = "oir_invocation_commit_guard"
_INVOCATION_CONNECTION_COMMIT_GUARD_KEY = "oir_invocation_connection_commit_guard"
_INVOCATION_ENGINE_COMMIT_LISTENERS_KEY = "oir_invocation_engine_commit_listeners"


def guard_invocation_commit(
    session: AsyncSession,
    may_commit: Callable[[], bool] | None,
) -> None:
    """Bind an Invocation deadline guard to the transaction's real commit.

    Repository code can check before a flush, but SQLAlchemy commits only when
    the ``session.begin`` context exits. ``before_commit`` is early enough to
    bind the guard to the transaction's actual Connection; its transaction-
    specific Engine ``commit`` callback then rechecks immediately before the
    dialect calls the DBAPI commit method. This covers time spent flushing
    pending ORM writes.
    """

    if may_commit is not None:
        session.info[_INVOCATION_COMMIT_GUARD_KEY] = may_commit


@event.listens_for(Session, "before_commit")
def _require_invocation_commit_guard(session: Session) -> None:
    guard = session.info.get(_INVOCATION_COMMIT_GUARD_KEY)
    if guard is None:
        return
    if not guard():
        raise InvocationDeadlineExceededError()
    # ``Session.connection`` returns the synchronous Connection participating
    # in this exact Session transaction. Register the final guard on that
    # Engine only now, after existing engine listeners, so it runs after an
    # implicit flush and immediately before DBAPI ``commit``. It still checks
    # the exact Connection identity, so another transaction sharing the
    # Engine cannot consume this Invocation's callback.
    connection = session.connection()
    connection.info[_INVOCATION_CONNECTION_COMMIT_GUARD_KEY] = guard
    engine = connection.engine

    def require_at_engine_commit(candidate: Connection) -> None:
        if candidate is connection:
            _require_invocation_connection_commit_guard(candidate)

    event.listen(engine, "commit", require_at_engine_commit)
    session.info.setdefault(_INVOCATION_ENGINE_COMMIT_LISTENERS_KEY, []).append(
        (engine, require_at_engine_commit)
    )


def _require_invocation_connection_commit_guard(connection: Connection) -> None:
    """Abort a transaction whose budget expired after Session hooks/flush."""

    guard = connection.info.pop(_INVOCATION_CONNECTION_COMMIT_GUARD_KEY, None)
    if guard is not None and not guard():
        raise InvocationDeadlineExceededError()


def _clear_invocation_connection_commit_guard(connection: Connection) -> None:
    """Never leak a transaction-scoped callback through a pooled connection."""

    connection.info.pop(_INVOCATION_CONNECTION_COMMIT_GUARD_KEY, None)


@event.listens_for(Session, "after_transaction_end")
def _clear_invocation_engine_commit_guards(session: Session, transaction: object) -> None:
    """Unregister the transaction-local Engine callbacks after commit/rollback."""

    if getattr(transaction, "parent", None) is not None:
        return
    listeners = session.info.pop(_INVOCATION_ENGINE_COMMIT_LISTENERS_KEY, ())
    for engine, listener in listeners:
        if event.contains(engine, "commit", listener):
            event.remove(engine, "commit", listener)
