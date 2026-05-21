# Active Context

## Current Focus

- Maintain project context after adding early-release tracking and update
  checking to the CLI.

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
  - Committed the implementation as `1c47a4a feat(cli): track early-release
    updates`.
- In progress:
  - Refreshing and committing memory-bank context for the completed
    early-release tracking work.
- Not started:
  - No further early-release tracking changes are currently requested.

## Recent Context

- The project is a Poetry-managed Python CLI for downloading authorized
  O'Reilly book assets and rebuilding EPUBs.
- Main implementation files are in `src/oreilly_library/`.
- The working tree had pre-existing modified/untracked files before the memory
  bank was created; those should be treated as unrelated unless the user says
  otherwise.
- Some upstream Wiley/O'Reilly source packages use `.htm` chapter files rather
  than `.html` or `.xhtml`; builder HTML handling should keep all three suffixes
  in sync.
- Early-release tracking depends on O'Reilly metadata fields `roughcut` and
  `last_modified_time` and uses the same authenticated session path as normal
  metadata fetches.

## Next Steps

- Include `memory-bank/notes/historical-user-prompts.txt` in any memory-bank
  commit.
- If committing this memory-bank refresh, stage only relevant `memory-bank/`
  files and leave unrelated local changes unstaged.
- For future development tasks, read this file and `progress.md` first.
