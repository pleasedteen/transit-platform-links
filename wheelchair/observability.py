"""Processing meters and API errors."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


@dataclass
class StopMeter:
    """Account for every stop_time record touched by an operation.

    ``db_reads`` counts rows materialized from SQLite.  ``objects_built``
    counts in-memory stop-shaped records generated from input or query rows.
    ``rows_written`` counts inserted/updated/deleted stop_time rows.

    The conservative ``accounted_touches`` sums all three categories.  The
    stress test asserts this total is at most 5,000 for each measured query,
    ruling, and undo.
    """

    operation: str = "operation"
    db_reads: int = 0
    objects_built: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_deleted: int = 0
    sql_statements: int = 0
    notes: list[str] = field(default_factory=list)

    def add_read_rows(self, count: int) -> None:
        self.db_reads += int(count)
        # Each materialized row is turned into exactly one domain row/dict.
        self.objects_built += int(count)

    def add_generated(self, count: int = 1) -> None:
        self.objects_built += int(count)

    def add_inserted(self, count: int = 1) -> None:
        count = int(count or 0)
        self.rows_inserted += count
        self.objects_built += count

    def add_updated(self, count: int = 1) -> None:
        self.rows_updated += int(count or 0)

    def add_deleted(self, count: int = 1) -> None:
        self.rows_deleted += int(count or 0)

    def add_statement(self) -> None:
        self.sql_statements += 1

    @property
    def rows_written(self) -> int:
        return self.rows_inserted + self.rows_updated + self.rows_deleted

    @property
    def accounted_touches(self) -> int:
        # Reads already include one built object in add_read_rows.  Input
        # generation and writes are also work, so report the conservative sum.
        return self.db_reads + self.objects_built + self.rows_written

    def report(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "stop_time_db_reads": self.db_reads,
            "stop_objects_built": self.objects_built,
            "stop_rows_inserted": self.rows_inserted,
            "stop_rows_updated": self.rows_updated,
            "stop_rows_deleted": self.rows_deleted,
            "stop_rows_written": self.rows_written,
            "accounted_stop_touches": self.accounted_touches,
            "sql_statements": self.sql_statements,
            "background_stop_processing": 0,
            "notes": self.notes,
        }
