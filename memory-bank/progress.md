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
- Package exports expose `EpubBuilder`, `EpubDownloader`, and `DownloadResult`.

## In Flight

- The `.htm` builder fix is complete and committed as `52616fb fix(epub):
  include htm documents in builder`.
- Memory-bank context is being refreshed to record the `.htm` fix and related
  investigation findings.
- The working tree contains pre-existing local changes and untracked files not
  created by the `.htm` fix or memory-bank refresh.

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
  `epubcheck` and Calibre.
- Authentication workflows depend on O'Reilly session behavior and ChromeDriver
  compatibility.
