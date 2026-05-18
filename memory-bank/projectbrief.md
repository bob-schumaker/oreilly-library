# Project Brief

## Goal

`oreilly-library` downloads assets for books available through an authenticated
O'Reilly Learning account and rebuilds them into local EPUB files.

## Scope

- Fetch book metadata and related JSON documents from O'Reilly API endpoints.
- Download chapter XHTML, images, stylesheets, fonts, and related assets.
- Normalize downloaded content so it can be packaged locally.
- Build EPUB archives with `ebooklib`.
- Support rebuilding an EPUB from an already downloaded local extraction.
- Optionally run validation or cleanup tools such as `epubcheck` and Calibre's
  `ebook-polish` when available.

## Out of Scope

- Granting access to content the user is not authorized to download.
- Circumventing O'Reilly authentication or licensing restrictions.
- Providing a general-purpose EPUB editor beyond the cleanup needed for this
  downloader/builder workflow.

## Constraints

- The tool requires an authenticated O'Reilly session or cookies from a valid
  account.
- Interactive login currently depends on Chrome/Selenium.
- The project targets Python 3.14+ and is configured with Poetry.
- Output quality depends on upstream O'Reilly source assets and may need
  book-specific cleanup for malformed content.
