"""SQLite persistence for O'Reilly early-release book tracking."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


DEFAULT_DATABASE_PATH = Path.home() / ".cache" / "oreilly-early-release.db"


@dataclass(frozen=True)
class TrackedBook:
    """A book currently tracked as an early release."""

    book_id: str
    book_title: str
    last_modified_time: str


@dataclass(frozen=True)
class UpdatedBook:
    """A tracked book with a newer remote ``last_modified_time`` value."""

    tracked: TrackedBook
    remote: TrackedBook


class EarlyReleaseTracker:
    """Store and query early-release book metadata in SQLite."""

    def __init__(self, database_path: Path | str = DEFAULT_DATABASE_PATH) -> None:
        self.database_path = Path(database_path).expanduser()

    def initialize(self) -> None:
        """Create the tracking table if it does not already exist."""

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS early_release_books (
                    book_id TEXT PRIMARY KEY,
                    book_title TEXT NOT NULL,
                    last_modified_time TEXT NOT NULL
                )
                """
            )

    def upsert(self, book: TrackedBook) -> None:
        """Insert or update a tracked early-release book."""

        self.initialize()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO early_release_books (
                    book_id,
                    book_title,
                    last_modified_time
                )
                VALUES (?, ?, ?)
                ON CONFLICT(book_id) DO UPDATE SET
                    book_title = excluded.book_title,
                    last_modified_time = excluded.last_modified_time
                """,
                (book.book_id, book.book_title, book.last_modified_time),
            )

    def delete(self, book_id: str) -> None:
        """Remove a book from early-release tracking."""

        self.initialize()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM early_release_books WHERE book_id = ?",
                (book_id,),
            )

    def list_books(self) -> list[TrackedBook]:
        """Return all tracked early-release books in title order."""

        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT book_id, book_title, last_modified_time
                FROM early_release_books
                ORDER BY book_title COLLATE NOCASE, book_id
                """
            ).fetchall()
        return [TrackedBook(*row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path)


def tracked_book_from_metadata(
    metadata: Mapping[str, Any],
    *,
    fallback_identifier: Optional[str] = None,
) -> Optional[TrackedBook]:
    """Create a :class:`TrackedBook` from O'Reilly metadata when possible."""

    book_id = _first_text(metadata, "identifier", "id", "isbn") or fallback_identifier
    book_title = _first_text(metadata, "title", "name")
    last_modified_time = _first_text(metadata, "last_modified_time")

    if not book_id or not book_title or not last_modified_time:
        return None

    return TrackedBook(
        book_id=book_id,
        book_title=book_title,
        last_modified_time=last_modified_time,
    )


def metadata_is_roughcut(metadata: Mapping[str, Any]) -> bool:
    """Return whether O'Reilly metadata marks a book as a roughcut."""

    return metadata.get("roughcut") is True


def find_updated_books(
    tracked_books: Iterable[TrackedBook],
    remote_books: Mapping[str, TrackedBook],
) -> list[UpdatedBook]:
    """Compare tracked rows with remote metadata snapshots."""

    updated_books: list[UpdatedBook] = []
    for tracked in tracked_books:
        remote = remote_books.get(tracked.book_id)
        if remote is None:
            continue
        if remote.last_modified_time != tracked.last_modified_time:
            updated_books.append(UpdatedBook(tracked=tracked, remote=remote))
    return updated_books


def _first_text(metadata: Mapping[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int):
            return str(value)
    return None


__all__ = [
    "DEFAULT_DATABASE_PATH",
    "EarlyReleaseTracker",
    "TrackedBook",
    "UpdatedBook",
    "find_updated_books",
    "metadata_is_roughcut",
    "tracked_book_from_metadata",
]
