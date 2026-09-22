from __future__ import annotations

import sqlite3
from types import TracebackType
from typing import Literal


class ImmediateTransaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> Literal[False]:
        if exception_type is None:
            self._connection.commit()
        else:
            self._connection.rollback()
        return False
