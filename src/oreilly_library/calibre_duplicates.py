#!/usr/bin/env python3
"""Quarantine incomplete duplicate Calibre EPUBs.

The script only selects books that have the same normalized title and ISBN. It
removes a copy only when its normalized visible text is a strict subset of the
other copy.

Run without ``--apply`` first.  Applying creates a metadata.db backup and moves
the obsolete book folders into a timestamped quarantine directory inside the
library before deleting their Calibre records.
"""

from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import posixpath
import json
import shutil
import sqlite3
import sys
import unicodedata
from urllib.parse import unquote, urlsplit
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence
from xml.etree import ElementTree


@dataclass(frozen=True)
class Duplicate:
    """An older Calibre record eligible for removal."""

    book_id: int
    title: str
    isbn: str
    timestamp: str
    path: str
    kept_book_id: int
    group_count: int


def find_duplicates(database: Path) -> list[Duplicate]:
    """Return same-title, same-ISBN duplicate candidates from a Calibre database."""

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            WITH isbn_groups AS (
                SELECT
                    lower(trim(b.title)) AS title_key,
                    lower(trim(i.val)) AS isbn,
                    count(*) AS copies
                FROM books AS b
                JOIN identifiers AS i
                  ON i.book = b.id
                 AND lower(i.type) = 'isbn'
                 AND trim(i.val) <> ''
                GROUP BY title_key, isbn
                HAVING copies > 1
            ), ranked AS (
                SELECT
                    b.id,
                    b.title,
                    b.path,
                    b.timestamp,
                    lower(trim(i.val)) AS isbn,
                    g.copies AS group_count,
                    row_number() OVER (
                        PARTITION BY lower(trim(b.title)), lower(trim(i.val))
                        ORDER BY b.timestamp DESC, b.id DESC
                    ) AS import_rank,
                    first_value(b.id) OVER (
                        PARTITION BY lower(trim(b.title)), lower(trim(i.val))
                        ORDER BY b.timestamp DESC, b.id DESC
                    ) AS kept_book_id
                FROM books AS b
                JOIN identifiers AS i
                  ON i.book = b.id
                 AND lower(i.type) = 'isbn'
                 AND trim(i.val) <> ''
                JOIN isbn_groups AS g
                  ON lower(trim(b.title)) = g.title_key
                 AND lower(trim(i.val)) = g.isbn
            )
            SELECT id, title, isbn, timestamp, path, kept_book_id, group_count
            FROM ranked
            WHERE import_rank > 1
            ORDER BY title COLLATE NOCASE, isbn, timestamp, id
            """
        ).fetchall()

    return [
        Duplicate(
            book_id=row["id"],
            title=row["title"],
            isbn=row["isbn"],
            timestamp=row["timestamp"],
            path=row["path"],
            kept_book_id=row["kept_book_id"],
            group_count=row["group_count"],
        )
        for row in rows
    ]


BLOCK_ELEMENTS = {
    "article",
    "blockquote",
    "div",
    "figcaption",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "p",
    "pre",
    "section",
}


class VisibleTextParser(HTMLParser):
    """Extract visible XHTML text while retaining paragraph boundaries."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._hidden_depth += 1
        if tag in BLOCK_ELEMENTS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._hidden_depth:
            self._hidden_depth -= 1
        if tag in BLOCK_ELEMENTS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self._chunks.append(data)

    def blocks(self) -> list[str]:
        return [normalize_text(block) for block in "".join(self._chunks).splitlines()]


def normalize_text(text: str) -> str:
    """Normalize cosmetic markup differences without changing visible words."""

    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def archive_path(rootfile_path: str, href: str) -> str:
    """Resolve an OPF manifest href to a normalized archive member name."""

    href_path = unquote(urlsplit(href).path)
    return posixpath.normpath(
        posixpath.join(posixpath.dirname(rootfile_path), href_path)
    )


def epub_text_fingerprints(book_directory: Path) -> frozenset[str] | None:
    """Return normalized chapter and paragraph fingerprints from a sole EPUB."""

    epubs = list(book_directory.glob("*.epub"))
    if len(epubs) != 1:
        return None
    try:
        with zipfile.ZipFile(epubs[0]) as archive:
            container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
            rootfile = next(
                element.attrib["full-path"]
                for element in container.iter()
                if element.tag.endswith("rootfile")
            )
            opf = ElementTree.fromstring(archive.read(rootfile))
            manifest = {
                element.attrib["id"]: archive_path(rootfile, element.attrib["href"])
                for element in opf.iter()
                if element.tag.endswith("item")
                and "id" in element.attrib
                and "href" in element.attrib
            }
            spine_paths = [
                manifest[element.attrib["idref"]]
                for element in opf.iter()
                if element.tag.endswith("itemref")
                and element.attrib.get("idref") in manifest
            ]
            fingerprints: set[str] = set()
            for spine_path in spine_paths:
                parser = VisibleTextParser()
                parser.feed(archive.read(spine_path).decode("utf-8"))
                blocks = [block for block in parser.blocks() if block]
                if blocks:
                    fingerprints.add(fingerprint("\n".join(blocks)))
                    fingerprints.update(fingerprint(block) for block in blocks)
            return frozenset(fingerprints) or None
    except (
        KeyError,
        UnicodeDecodeError,
        ElementTree.ParseError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        return None


def select_incomplete_duplicates(library: Path, database: Path) -> list[Duplicate]:
    """Return only pairwise duplicates with demonstrably missing EPUB text."""

    selected: list[Duplicate] = []
    for duplicate in find_duplicates(database):
        if duplicate.group_count != 2:
            continue
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT timestamp, path FROM books WHERE id = ?",
                (duplicate.kept_book_id,),
            ).fetchone()
        if row is None:
            continue

        duplicate_fingerprints = epub_text_fingerprints(
            book_directory(library, duplicate.path)
        )
        kept_fingerprints = epub_text_fingerprints(book_directory(library, row[1]))
        if not duplicate_fingerprints or not kept_fingerprints:
            continue
        if duplicate_fingerprints < kept_fingerprints:
            selected.append(duplicate)
        elif kept_fingerprints < duplicate_fingerprints:
            selected.append(
                Duplicate(
                    book_id=duplicate.kept_book_id,
                    title=duplicate.title,
                    isbn=duplicate.isbn,
                    timestamp=row[0],
                    path=row[1],
                    kept_book_id=duplicate.book_id,
                    group_count=duplicate.group_count,
                )
            )
    return selected


def book_directory(library: Path, relative_path: str) -> Path:
    """Resolve a Calibre relative book path without permitting path escape."""

    root = library.resolve()
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise ValueError(
            f"book path must be relative to the library: {relative_path!r}"
        )

    resolved = (root / candidate).resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"book path escapes the library: {relative_path!r}")
    return resolved


def validate_targets(library: Path, duplicates: Sequence[Duplicate]) -> list[Path]:
    """Ensure every selected record points to a real directory in the library."""

    directories = [book_directory(library, duplicate.path) for duplicate in duplicates]
    missing = [str(directory) for directory in directories if not directory.is_dir()]
    if missing:
        raise FileNotFoundError(
            "Refusing to modify metadata.db because selected book folders are missing:\n"
            + "\n".join(missing)
        )
    if len(set(directories)) != len(directories):
        raise ValueError("Refusing to remove records that share a book folder.")
    return directories


def apply_deletions(
    library: Path, duplicates: Sequence[Duplicate]
) -> tuple[Path, Path]:
    """Quarantine folders and delete their matching Calibre records.

    Returns the metadata backup path and the quarantine directory.
    """

    database = library / "metadata.db"
    directories = validate_targets(library, duplicates)
    run_name = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = library / f"metadata.db.before-duplicate-prune-{run_name}"
    quarantine = library / ".oreilly-duplicate-quarantine" / run_name
    shutil.copy2(database, backup)

    moved: list[tuple[Path, Path]] = []
    try:
        for duplicate, source in zip(duplicates, directories, strict=True):
            destination = quarantine / duplicate.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise FileExistsError(
                    f"quarantine destination already exists: {destination}"
                )
            shutil.move(str(source), str(destination))
            moved.append((source, destination))

        with sqlite3.connect(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "DELETE FROM books WHERE id = ?",
                [(duplicate.book_id,) for duplicate in duplicates],
            )
    except Exception:
        for source, destination in reversed(moved):
            if destination.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(destination), str(source))
        raise

    return backup, quarantine


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "library",
        type=Path,
        help="Calibre library directory containing metadata.db",
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="Write JSON report here"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    library = args.library.expanduser().resolve()
    database = library / "metadata.db"
    if not library.is_dir():
        print(f"Library directory does not exist: {library}", file=sys.stderr)
        return 2
    if not database.is_file():
        print(f"No Calibre metadata database found: {database}", file=sys.stderr)
        return 2

    try:
        candidates = find_duplicates(database)
        proven = select_incomplete_duplicates(library, database)
    except (sqlite3.DatabaseError, OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2

    report = {
        "schema_version": 1,
        "library": str(library),
        "candidates": [duplicate.__dict__ for duplicate in candidates],
        "proven_incomplete": [duplicate.__dict__ for duplicate in proven],
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(candidates)} candidate(s) to {args.output}.")
    print(f"Proven incomplete: {len(proven)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
