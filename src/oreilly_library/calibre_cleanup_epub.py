#!/usr/bin/env python3
"""
 Run it with Calibre's bundled Python runtime:

/Applications/calibre.app/Contents/MacOS/calibre-debug -e bin/calibre_cleanup_epub.py -- /path/to/
book.epub

By default it modifies the EPUB in place and creates /path/to/book.epub.bak. To write a separate
output:

/Applications/calibre.app/Contents/MacOS/calibre-debug -e bin/calibre_cleanup_epub.py -- /path/to/
book.epub -o /path/to/cleaned.epub

It runs, in order:

1. Remove unused CSS
2. Fix HTML in all files
3. Run Check Book
4. Auto-fix fixable check errors
5. Run Check Book again
6. Save the EPUB
"""

import argparse
import os
import shutil
import sys

from calibre.ebooks.oeb.polish.check.main import fix_errors, run_checks
from calibre.ebooks.oeb.polish.container import get_container
from calibre.ebooks.oeb.polish.css import remove_unused_css
from calibre.ebooks.oeb.polish.pretty import fix_all_html


def report_line(lines, message):
    lines.append(message)
    print(message)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=(
            "Run Calibre Edit Book cleanup operations on an EPUB: remove unused CSS, "
            "fix HTML in all files, run Check Book, and auto-fix fixable errors."
        )
    )
    parser.add_argument("epub", help="Path to the EPUB to modify")
    parser.add_argument(
        "-o",
        "--output",
        help="Write the cleaned EPUB to this path instead of modifying the input in place",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create a .bak copy when modifying the input in place",
    )
    parser.add_argument(
        "--remove-unused-classes",
        action="store_true",
        help="Also remove class attributes that do not match any CSS rule",
    )
    parser.add_argument(
        "--merge-identical-selectors",
        action="store_true",
        help="Merge CSS rules with identical selectors",
    )
    parser.add_argument(
        "--merge-identical-properties",
        action="store_true",
        help="Merge CSS rules with identical properties",
    )
    parser.add_argument(
        "--keep-unreferenced-sheets",
        action="store_true",
        help="Keep stylesheets that are not referenced by any content",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    epub = os.path.abspath(args.epub)
    output = os.path.abspath(args.output) if args.output else epub
    in_place = output == epub
    lines = []

    if not os.path.isfile(epub):
        raise SystemExit(f"EPUB not found: {epub}")
    if not epub.lower().endswith(".epub"):
        raise SystemExit(f"Expected an .epub file: {epub}")

    if in_place and not args.no_backup:
        backup = epub + ".bak"
        if os.path.exists(backup):
            raise SystemExit(f"Backup already exists, refusing to overwrite: {backup}")
        shutil.copy2(epub, backup)
        report_line(lines, f"Backup written: {backup}")

    container = get_container(epub, tweak_mode=True)

    css_changed = remove_unused_css(
        container,
        report=lines.append,
        remove_unused_classes=args.remove_unused_classes,
        merge_rules=args.merge_identical_selectors,
        merge_rules_with_identical_properties=args.merge_identical_properties,
        remove_unreferenced_sheets=not args.keep_unreferenced_sheets,
    )
    report_line(lines, f"Remove unused CSS changed book: {css_changed}")

    fix_all_html(container)
    report_line(lines, "Fixed HTML in all files")

    errors_before = run_checks(container)
    report_line(lines, f"Check Book errors before auto-fix: {len(errors_before)}")

    fixed = False
    if errors_before:
        fixed = fix_errors(container, errors_before)
        report_line(lines, f"Auto-fix changed book: {fixed}")

    errors_after = run_checks(container)
    report_line(lines, f"Check Book errors after auto-fix: {len(errors_after)}")

    container.commit(output)
    report_line(lines, f"Saved EPUB: {output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
