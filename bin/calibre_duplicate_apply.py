#!/usr/bin/env python3
"""Apply exact, user-approved Calibre duplicate decisions from JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from oreilly_library import calibre_duplicates as discovery


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("decisions", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    library = args.library.expanduser().resolve()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    decisions = json.loads(args.decisions.read_text(encoding="utf-8"))
    if report.get("library") != str(library):
        parser.error("report was generated for a different library")
    requested = decisions.get("decisions", [])
    candidates = {row["book_id"]: row for row in report.get("proven_incomplete", [])}
    if not requested:
        parser.error("decisions must contain at least one remove_id/keep_id pair")
    requested_ids = {decision.get("remove_id") for decision in requested}
    if len(requested_ids) != len(requested) or not requested_ids <= candidates.keys():
        parser.error(
            "every remove_id must be a unique proven-incomplete ID from the report"
        )
    if any(
        candidates[decision["remove_id"]]["kept_book_id"] != decision.get("keep_id")
        for decision in requested
    ):
        parser.error("every keep_id must match the report's proposed retained record")
    live_candidates = {
        duplicate.book_id: duplicate
        for duplicate in discovery.select_incomplete_duplicates(
            library, library / "metadata.db"
        )
    }
    if any(
        live_candidates.get(decision["remove_id"], None) is None
        or live_candidates[decision["remove_id"]].kept_book_id != decision["keep_id"]
        for decision in requested
    ):
        parser.error("live metadata no longer matches the report; run discovery again")
    duplicates = [live_candidates[book_id] for book_id in sorted(requested_ids)]
    if not args.apply:
        print(f"Would remove {len(duplicates)} record(s). Re-run with --apply.")
        return 0
    backup, quarantine = discovery.apply_deletions(library, duplicates)
    print(f"Removed {len(duplicates)} record(s).")
    print(f"Database backup: {backup}")
    print(f"Quarantined folders: {quarantine}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
