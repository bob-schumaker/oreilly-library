# Active Context

## Current Focus

- Maintain project context after fixing EPUB builder handling for books whose
  chapter documents use `.htm` extensions.

## Current Status

- Done:
  - Fixed `src/oreilly_library/epub_builder.py` so `.htm` documents are treated
    as XHTML/HTML documents throughout manifest generation, spine discovery,
    normalization, media-type detection, archive cleanup, and resource fallback
    handling.
  - Rebuilt `working/Books/Game Theory_ An Introduction, 2nd Edition
    (9781118533895)/9781118533895.epub` and confirmed it now includes 29 HTML
    documents, 27 `.htm` manifest entries, 4,593 image references, and no
    unresolved local image/resource references.
  - Investigated the 9 content-unreferenced images in that rebuilt EPUB and
    determined they are source/package baggage rather than missing image
    references; only `black_box.jpg` is a byte-identical duplicate of the
    referenced `black-box.jpg`.
  - Committed the builder fix as `52616fb fix(epub): include htm documents in
    builder`.
- In progress:
  - Refreshing and committing memory-bank context for the completed `.htm` fix.
- Not started:
  - No further builder changes are currently requested.

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

## Next Steps

- Include `memory-bank/notes/historical-user-prompts.txt` in any memory-bank
  commit.
- If committing this memory-bank refresh, stage only relevant `memory-bank/`
  files and leave unrelated local changes unstaged.
- For future development tasks, read this file and `progress.md` first.
