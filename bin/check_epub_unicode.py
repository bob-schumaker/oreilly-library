#!/usr/bin/env python3
"""Scan XHTML/HTML files inside an EPUB for encoding and Unicode issues.

An EPUB is a ZIP container, so decoding the `.epub` file itself as UTF-8 will fail.
This script opens the archive properly, inspects each XHTML/HTML member, and reports:

- decode failures using the declared encoding (or UTF-8 by default)
- suspicious Unicode characters such as replacement characters, surrogates,
  noncharacters, and disallowed control characters

Usage:
    python check_epub_unicode.py /path/to/book.epub
    python check_epub_unicode.py /path/to/book.epub --show-clean
    python check_epub_unicode.py /path/to/book.epub --strict
"""

from __future__ import annotations

import argparse
import codecs
import re
import sys
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


XML_ENCODING_RE = re.compile(
    rb"^\s*<\?xml[^>]*encoding=[\"']([^\"']+)[\"']", re.IGNORECASE
)
HTML_META_CHARSET_RE = re.compile(
    rb"<meta[^>]+charset=[\"']?\s*([A-Za-z0-9._-]+)", re.IGNORECASE
)
HTML_META_CONTENT_TYPE_RE = re.compile(
    rb"<meta[^>]+content=[\"'][^\"']*charset=([A-Za-z0-9._-]+)", re.IGNORECASE
)
HTML_EXTENSIONS = (".xhtml", ".html", ".htm")


@dataclass(frozen=True)
class DecodeIssue:
    file_name: str
    encoding: str
    source: str
    byte_offset: int
    reason: str
    byte_context_hex: str


@dataclass(frozen=True)
class CharacterIssue:
    file_name: str
    encoding: str
    source: str
    index: int
    line: int
    column: int
    code_point: int
    label: str
    name: str
    text_context: str


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Open an EPUB as a ZIP archive and scan XHTML/HTML members for "
            "encoding errors and suspicious Unicode characters."
        )
    )
    parser.add_argument("epub", type=Path, help="Path to the EPUB file to scan")
    parser.add_argument(
        "--show-clean",
        action="store_true",
        help="Print a line for files that scan cleanly",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with status 1 if any issue is found",
    )
    parser.add_argument(
        "--max-findings-per-file",
        type=int,
        default=20,
        help="Maximum suspicious character findings to print per file (default: 20)",
    )
    return parser.parse_args(argv)


def iter_html_members(archive: zipfile.ZipFile) -> Iterable[str]:
    for name in archive.namelist():
        lowered = name.lower()
        if lowered.endswith(HTML_EXTENSIONS):
            yield name


def detect_declared_encoding(data: bytes) -> tuple[str, str]:
    sample = data[:4096]

    xml_match = XML_ENCODING_RE.search(sample)
    if xml_match:
        return xml_match.group(1).decode("ascii", "replace"), "xml declaration"

    meta_match = HTML_META_CHARSET_RE.search(sample)
    if meta_match:
        return meta_match.group(1).decode("ascii", "replace"), "meta charset"

    content_match = HTML_META_CONTENT_TYPE_RE.search(sample)
    if content_match:
        return content_match.group(1).decode("ascii", "replace"), "meta content-type"

    return "utf-8", "default"


def normalize_encoding_name(encoding: str) -> str:
    return codecs.lookup(encoding).name


def is_noncharacter(code_point: int) -> bool:
    return 0xFDD0 <= code_point <= 0xFDEF or (code_point & 0xFFFF) in {0xFFFE, 0xFFFF}


def classify_character(character: str) -> str | None:
    code_point = ord(character)
    category = unicodedata.category(character)

    if character == "\ufffd":
        return "replacement character"
    if category == "Cs":
        return "surrogate code point"
    if category == "Cc" and character not in "\t\n\r":
        return "control character"
    if is_noncharacter(code_point):
        return "noncharacter code point"
    return None


def offset_to_line_column(text: str, index: int) -> tuple[int, int]:
    line = text.count("\n", 0, index) + 1
    line_start = text.rfind("\n", 0, index)
    column = index + 1 if line_start == -1 else index - line_start
    return line, column


def make_byte_context(data: bytes, byte_offset: int, radius: int = 8) -> str:
    start = max(0, byte_offset - radius)
    end = min(len(data), byte_offset + radius)
    return " ".join(f"{byte:02x}" for byte in data[start:end])


def make_text_context(text: str, index: int, radius: int = 25) -> str:
    start = max(0, index - radius)
    end = min(len(text), index + radius + 1)
    snippet = text[start:end]
    return snippet.encode("unicode_escape", "backslashreplace").decode("ascii")


def scan_text(
    file_name: str,
    encoding: str,
    source: str,
    text: str,
) -> list[CharacterIssue]:
    findings: list[CharacterIssue] = []
    for index, character in enumerate(text):
        label = classify_character(character)
        if label is None:
            continue

        line, column = offset_to_line_column(text, index)
        code_point = ord(character)
        findings.append(
            CharacterIssue(
                file_name=file_name,
                encoding=encoding,
                source=source,
                index=index,
                line=line,
                column=column,
                code_point=code_point,
                label=label,
                name=unicodedata.name(character, "<unnamed>"),
                text_context=make_text_context(text, index),
            )
        )
    return findings


def scan_epub(
    epub_path: Path,
) -> tuple[list[str], list[DecodeIssue], list[CharacterIssue], list[str]]:
    clean_files: list[str] = []
    decode_issues: list[DecodeIssue] = []
    character_issues: list[CharacterIssue] = []
    scanned_files: list[str] = []

    with zipfile.ZipFile(epub_path) as archive:
        for member_name in iter_html_members(archive):
            scanned_files.append(member_name)
            raw = archive.read(member_name)
            declared_encoding, source = detect_declared_encoding(raw)

            try:
                encoding = normalize_encoding_name(declared_encoding)
            except LookupError:
                decode_issues.append(
                    DecodeIssue(
                        file_name=member_name,
                        encoding=declared_encoding,
                        source=source,
                        byte_offset=0,
                        reason=f"unknown encoding declaration: {declared_encoding}",
                        byte_context_hex=make_byte_context(raw, 0),
                    )
                )
                continue

            try:
                text = raw.decode(encoding)
            except UnicodeDecodeError as exc:
                decode_issues.append(
                    DecodeIssue(
                        file_name=member_name,
                        encoding=encoding,
                        source=source,
                        byte_offset=exc.start,
                        reason=str(exc),
                        byte_context_hex=make_byte_context(raw, exc.start),
                    )
                )
                continue

            findings = scan_text(member_name, encoding, source, text)
            if findings:
                character_issues.extend(findings)
            else:
                clean_files.append(member_name)

    return clean_files, decode_issues, character_issues, scanned_files


def print_decode_issue(issue: DecodeIssue) -> None:
    print(f"[DECODE ERROR] {issue.file_name}")
    print(f"  encoding: {issue.encoding} ({issue.source})")
    print(f"  byte offset: {issue.byte_offset}")
    print(f"  reason: {issue.reason}")
    print(f"  bytes: {issue.byte_context_hex}")


def print_character_issue(issue: CharacterIssue) -> None:
    print(f"[SUSPICIOUS CHARACTER] {issue.file_name}")
    print(f"  encoding: {issue.encoding} ({issue.source})")
    print(f"  position: line {issue.line}, column {issue.column}, index {issue.index}")
    print(f"  code point: U+{issue.code_point:04X} {issue.name}")
    print(f"  type: {issue.label}")
    print(f"  context: {issue.text_context}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    epub_path = args.epub.expanduser().resolve()

    if not epub_path.exists():
        print(f"EPUB not found: {epub_path}", file=sys.stderr)
        return 2
    if not epub_path.is_file():
        print(f"Not a file: {epub_path}", file=sys.stderr)
        return 2

    try:
        clean_files, decode_issues, character_issues, scanned_files = scan_epub(
            epub_path
        )
    except zipfile.BadZipFile as exc:
        print(f"Not a valid EPUB/ZIP file: {epub_path}", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2

    print(f"Scanned EPUB: {epub_path}")
    print(f"Checked XHTML/HTML files: {len(scanned_files)}")

    for issue in decode_issues:
        print_decode_issue(issue)

    if character_issues:
        current_file = None
        emitted_for_file = 0
        for issue in character_issues:
            if issue.file_name != current_file:
                current_file = issue.file_name
                emitted_for_file = 0
            if emitted_for_file >= args.max_findings_per_file:
                continue
            print_character_issue(issue)
            emitted_for_file += 1

    if args.show_clean:
        for file_name in clean_files:
            print(f"[OK] {file_name}")

    if decode_issues or character_issues:
        print(
            "Summary: "
            f"{len(decode_issues)} decode error(s), "
            f"{len(character_issues)} suspicious character(s)"
        )
        return 1 if args.strict else 0

    print("Summary: no decode errors or suspicious Unicode characters found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
