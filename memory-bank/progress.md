# Progress

## Working

- README-documented CLI can download/build books with authenticated cookies or
  interactive Chrome/Selenium login.
- `--build` mode rebuilds from already downloaded local files.
- `EpubDownloader` handles metadata, related documents, chapter aggregation,
  asset download, and cover fallback behavior.
- `EpubBuilder` assembles EPUBs from local folders and performs metadata,
  manifest, spine, TOC, XHTML/CSS, cover, and optional cleanup/validation work.
- `EpubBuilder` now treats `.htm`, `.html`, and `.xhtml` files consistently as
  XHTML/HTML content so source packages with `.htm` chapters are included in the
  EPUB manifest/spine and have their image references normalized.
- Early-release tracking works for downloaded books whose metadata has
  `roughcut == True`, using `~/.cache/oreilly-early-release.db` to store book id,
  title, and last modified time.
- CLI `--check` lists tracked early-release books with changed remote
  `last_modified_time`; `--check --fetch` fetches updated books and refreshes
  tracker state.
- During `--check`, tracked books that return HTTP 404 are removed from the
  SQLite tracker and checking continues.
- During `--check`, removal/update messages are collected during the progress
  loop and displayed after iteration, avoiding progress bar disruption.
- The previous EPUB validation option is now `--epubcheck`.
- Package exports expose `EpubBuilder`, `EpubDownloader`, and `DownloadResult`.

## In Flight

- No active implementation work is currently in progress.
- The working tree contains pre-existing local changes and untracked files not
  created by the early-release tracking work or memory-bank refresh.

## Remaining

- Keep this memory bank current after meaningful feature, bugfix, or workflow
  milestones.
- Include `memory-bank/notes/historical-user-prompts.txt` whenever memory-bank
  changes are committed.
- Use task-specific `cline-tasks/` handoffs for complex pause/resume state when
  branch status, validation commands, integration strategy, or commit boundaries
  matter.

## Risks or Follow-ups

- Some books may have malformed upstream content that requires additional
  cleanup logic.
- Some source packages may include image files that are present in the OPF
  manifest but never referenced by renderable content; this can be harmless
  source/package baggage rather than a display bug.
- External validation and cleanup depend on locally installed tools such as
  `epubcheck` and Calibre. The CLI flag for validation is now `--epubcheck`.
- Authentication workflows depend on O'Reilly session behavior and ChromeDriver
  compatibility.
- Early-release update checks require authenticated O'Reilly metadata access;
  direct download tracking was verified with book id `9781098145842`.
- Calibre-style early-release JSON snapshots may include rows without ISBNs and
  duplicate ISBNs; the database refresh used ISBN as the tracker key, skipped
  missing ISBNs, and kept the newest duplicate metadata by `last_modified`.
