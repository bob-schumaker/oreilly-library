# Active Context

## Current Focus

- Maintain project context after recent CLI, Calibre cleanup, and packaging
  maintenance work.

## Current Status

- Done:
  - Added `src/oreilly_library/early_release_tracker.py` with SQLite persistence
    at `~/.cache/oreilly-early-release.db` for `book_id`, `book_title`, and
    `last_modified_time`.
  - Repurposed CLI `--check` to check tracked early-release books for updated
    remote `last_modified_time` values, and added `--fetch` to fetch changed
    books.
  - Renamed the previous EPUB validation flag from `--check` to `--epubcheck`.
  - Wired direct download/build metadata sync so `roughcut == True` upserts the
    tracker row and `roughcut != True` removes any existing row.
  - Verified real metadata and direct-download tracking with early-release book
    `9781098145842` (`High Performance Spark, 2nd Edition`).
  - Updated `/Users/roschuma/.cache/oreilly-early-release.db` from
    `/Users/roschuma/Downloads/early_release.json`: loaded 69 valid unique ISBN
    rows, skipped 7 rows without ISBNs, and kept newest metadata for 2 duplicate
    ISBNs. A database backup was created at
    `/Users/roschuma/.cache/oreilly-early-release.20260527T212933Z.db.bak`.
  - Changed `oreilly-library --check` so tracked books that return HTTP 404 are
    removed from tracking instead of aborting the whole check.
  - Changed `oreilly-library --check` to collect check results during the
    progress loop and print them after iteration so the progress bar is not
    disrupted.
  - Refined `oreilly-library --check` so it displays only books with changed
    remote `last_modified_time`; 404 and non-roughcut removals are silent in
    check output.
  - Confirmed tracked timestamps are not updated by plain `--check`; timestamps
    are refreshed only after `--check --fetch` downloads the updated book.
  - Gated early-release 404 tracker pruning behind `--check --clean`; plain
    `--check` leaves 404 rows tracked.
  - Vendored `oreilly_library.cobblerslib` so the package no longer depends on
    the private `cobblers` dependency.
  - Added a bundled Calibre cleanup script and wired `--clean` to run it through
    `calibre-debug` before `ebook-polish`.
  - Committed the latest functional change as `db6f4f5 Gate early-release 404
    pruning behind clean`.
- In progress:
  - No active implementation work is currently in progress.
- Not started:
  - No further early-release tracking changes are currently requested.

## Recent Context

- The project is a Poetry-managed Python CLI for downloading authorized
  O'Reilly book assets and rebuilding EPUBs.
- Main implementation files are in `src/oreilly_library/`.
- The working tree currently has modified `poetry.lock` and `pyproject.toml`
  plus untracked `bin/` and `technical.db`; treat them as unrelated unless the
  user says otherwise.
- Some upstream Wiley/O'Reilly source packages use `.htm` chapter files rather
  than `.html` or `.xhtml`; builder HTML handling should keep all three suffixes
  in sync.
- Early-release tracking depends on O'Reilly metadata fields `roughcut` and
  `last_modified_time` and uses the same authenticated session path as normal
  metadata fetches.
- The JSON database refresh mapped `isbn` -> `book_id`, `title` ->
  `book_title`, and `last_modified` -> `last_modified_time`.
- `--clean` has two meanings by mode: in build/download mode it runs Calibre EPUB
  cleanup, while with `--check` it allows pruning tracked rows whose metadata
  returns 404.

## Next Steps

- For future development tasks, read this file and `progress.md` first.
- Keep unrelated local changes unstaged unless the user explicitly scopes them
  into a task.
