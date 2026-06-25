# System Patterns

## High-Level Architecture

- `src/oreilly_library/__main__.py` provides the CLI entry point, argument
  parsing, cookie handling, optional browser-tab discovery on macOS, and routing
  between download/build modes.
- `src/oreilly_library/epub_downloader.py` owns authenticated API access,
  metadata aggregation, related-document pagination, asset downloads, cover
  fallback handling, and local download-folder preparation.
- `src/oreilly_library/early_release_tracker.py` owns the lightweight SQLite
  persistence layer for early-release tracking.
- `src/oreilly_library/epub_builder.py` owns EPUB assembly from a local source
  directory, including metadata extraction, manifest generation, TOC/spine
  construction, XHTML/CSS normalization, cover handling, archive cleanup, and
  optional external validation/cleanup.
- `src/oreilly_library/calibre_cleanup_epub.py` is a bundled helper script run
  by Calibre's Python runtime for Edit Book-style EPUB cleanup.
- `src/oreilly_library/cobblerslib.py` vendors the CLI/logging helper behavior
  previously supplied by the private `cobblers` package.
- `src/oreilly_library/__init__.py` exposes `EpubBuilder`, `EpubDownloader`, and
  `DownloadResult` as the public package interface.

## Main Data Flow

1. CLI parses options and identifiers.
2. Unless `--build` is used, the CLI creates an authenticated `requests.Session`
   from a cookie file or Selenium-captured cookies.
3. `EpubDownloader` fetches O'Reilly metadata and related JSON documents.
4. `EpubDownloader` downloads static assets into a per-book working directory.
5. `EpubBuilder` reads that working directory and writes an EPUB archive.
6. CLI metadata sync records `roughcut == True` books in the early-release
   tracker and removes rows for books that are no longer roughcuts.
7. Optional post-processing runs the Calibre cleanup helper, `ebook-polish`,
   and/or `epubcheck`.

## Design Patterns and Conventions

- Builder and downloader responsibilities are separated so existing local assets
  can be rebuilt without network access.
- Downloaded assets are normalized into predictable subdirectories such as
  `xhtml/`, `Images/`, `Styles/`, and `fonts/`.
- Builder HTML document handling is centralized around the supported suffix set
  `.htm`, `.html`, and `.xhtml`; keep discovery, manifest registration,
  normalization, media-type detection, and archive cleanup in sync when changing
  that set.
- Pagination handling aggregates API payloads before persisting local JSON.
- Early-release tracking intentionally stores only the user-requested minimal
  fields: book id, book title, and remote `last_modified_time`.
- The CLI uses `--check` for early-release update checks; EPUB validation is
  exposed separately as `--epubcheck`.
- In early-release check mode, 404 tracker pruning is a cleanup operation gated
  behind `--clean`; plain `--check` does not mutate rows solely because metadata
  is missing.
- In EPUB build/download mode, `--clean` first invokes the bundled Calibre
  cleanup script through `calibre-debug`, then runs `ebook-polish` when found.
- Warnings are collected and emitted by the builder rather than immediately
  aborting for every missing optional asset.
- Public APIs accept `Path` or string-like paths where practical and normalize
  them internally with `pathlib.Path`.

## Current Observations

- `example/` contains a sample extracted book tree and generated EPUB-related
  metadata/assets.
- `prompts.md` records prior task prompts and investigation context, not formal
  product documentation.
