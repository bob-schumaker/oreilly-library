from __future__ import annotations

import sqlite3
import tempfile
import zipfile
from pathlib import Path
from unittest import TestCase


from oreilly_library import calibre_duplicates as pruner


class PruneEarlyReleaseDuplicatesTests(TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.library = Path(self.temporary_directory.name)
        self.database = self.library / "metadata.db"
        with sqlite3.connect(self.database) as connection:
            connection.executescript(
                """
                CREATE TABLE books (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    path TEXT NOT NULL
                );
                CREATE TABLE identifiers (book INTEGER, type TEXT, val TEXT);
                CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE books_tags_link (book INTEGER, tag INTEGER);
                """
            )
            connection.execute("INSERT INTO tags VALUES (1, 'early release')")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def add_book(
        self,
        book_id: int,
        title: str,
        timestamp: str,
        isbn: str,
        *,
        early: bool,
        chapters: tuple[str, ...] = (),
    ) -> None:
        path = f"Author/{title} ({book_id})"
        (self.library / path).mkdir(parents=True)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO books VALUES (?, ?, ?, ?)",
                (book_id, title, timestamp, path),
            )
            connection.execute(
                "INSERT INTO identifiers VALUES (?, 'isbn', ?)", (book_id, isbn)
            )
            if early:
                connection.execute(
                    "INSERT INTO books_tags_link VALUES (?, 1)", (book_id,)
                )
        with zipfile.ZipFile(self.library / path / "book.epub", "w") as archive:
            archive.writestr(
                "META-INF/container.xml",
                "<?xml version='1.0'?><container><rootfiles>"
                "<rootfile full-path='OEBPS/content.opf'/></rootfiles></container>",
            )
            manifest = "".join(
                f"<item id='chapter-{index}' href='chapter-{index}.xhtml'/>"
                for index, _ in enumerate(chapters, start=1)
            )
            spine = "".join(
                f"<itemref idref='chapter-{index}'/>"
                for index, _ in enumerate(chapters, start=1)
            )
            archive.writestr(
                "OEBPS/content.opf",
                f"<package><manifest>{manifest}</manifest><spine>{spine}</spine></package>",
            )
            for index, chapter in enumerate(chapters, start=1):
                archive.writestr(
                    f"OEBPS/chapter-{index}.xhtml",
                    f"<html><body><p>{chapter}</p></body></html>",
                )

    def test_selects_same_title_and_isbn_without_early_release_tag(self) -> None:
        self.add_book(
            1,
            "Book",
            "2026-01-01",
            "9780000000001",
            early=False,
            chapters=("First chapter",),
        )
        self.add_book(
            2,
            "Book",
            "2026-02-01",
            "9780000000001",
            early=False,
            chapters=("First chapter", "Second chapter"),
        )
        self.add_book(3, "Book", "2026-03-01", "9780000000002", early=True)

        duplicates = pruner.find_duplicates(self.database)

        self.assertEqual(
            duplicates,
            [
                pruner.Duplicate(
                    book_id=1,
                    title="Book",
                    isbn="9780000000001",
                    timestamp="2026-01-01",
                    path="Author/Book (1)",
                    kept_book_id=2,
                    group_count=2,
                )
            ],
        )

    def test_apply_drops_the_epub_with_missing_content(self) -> None:
        self.add_book(
            1,
            "Book",
            "2026-01-01",
            "9780000000001",
            early=False,
            chapters=("First chapter",),
        )
        self.add_book(
            2,
            "Book",
            "2026-02-01",
            "9780000000001",
            early=False,
            chapters=("First chapter", "Second chapter"),
        )
        duplicates = pruner.select_incomplete_duplicates(self.library, self.database)

        backup, quarantine = pruner.apply_deletions(self.library, duplicates)

        self.assertTrue(backup.is_file())
        self.assertTrue((quarantine / "Author/Book (1)").is_dir())
        self.assertFalse((self.library / "Author/Book (1)").exists())
        self.assertTrue((self.library / "Author/Book (2)").is_dir())
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute("SELECT id FROM books").fetchall(), [(2,)]
            )
