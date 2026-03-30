# oreilly-library

Download EPUB assets from the O'Reilly learning platform and rebuild them into
local `.epub` files.

This project uses an authenticated O'Reilly session to fetch book metadata,
chapters, images, stylesheets, fonts, navigation data, and related resources.
It then normalizes the downloaded content and assembles a clean EPUB archive
with `ebooklib`.

> [!IMPORTANT]
> Use this tool only with content you are authorized to access through your own
> O'Reilly account.

## Features

- Download all assets needed to reconstruct an O'Reilly book locally
- Rebuild books into a standards-friendly EPUB archive
- Normalize downloaded XHTML/CSS and repair common markup issues
- Preserve metadata such as title, authors, publishers, TOC, and spine order
- Generate a synthetic cover page when a cover document is missing
- Optionally run `epubcheck` after building
- Reuse a saved cookies file or launch a Selenium-based login flow

## Requirements

- Python 3.14+
- An O'Reilly account with access to the target book(s)
- Google Chrome for interactive login
- A compatible Chrome WebDriver available in `PATH` when using Selenium login
- Poetry for dependency installation

## Installation

This repository is configured for Poetry.

```bash
poetry install
```

The project depends on `cobblerslib`, which is configured as a supplemental
package source in `pyproject.toml`. If installation fails, verify that you can
reach the configured package index.

## Authentication

The downloader needs authenticated O'Reilly cookies.

You can provide them in one of two ways:

1. **Use an existing cookie file** with `--cookie-file`
2. **Let the tool collect cookies with Selenium**

If no cookie file is available, the CLI opens Chrome, waits for you to log in,
and saves the captured cookies to disk.

Supported cookie formats:

- A list of browser cookie objects (for example from Chrome extensions)
- A simple name/value cookie mapping

### Related environment variables

- `SAFARICOOKIES_PATH`: default cookie file path
- `SAFARIBOOKS_PATH`: base working directory used when `--output-dir` is not set
- `OREILLY_LOGIN_URL`: override the login page opened by Selenium

## Command-line usage

```text
oreilly-library [--verbose] [--debug] [--check] [--output-dir=OUTPUT] \
                [--cookie-file=FILE] [--browser=BROWSER] [--login] ISBN...
```

### Arguments

- `ISBN`: one or more O'Reilly identifiers / ISBN-like identifiers to download

### Options

- `--output-dir=OUTPUT`: write downloaded assets and generated EPUBs here
- `--cookie-file=FILE`: load cookies from a JSON file
- `--browser=BROWSER`: browser to use for Selenium login (`chrome` only)
- `--login`: force a fresh Selenium login even if cookies already exist
- `--check`: run `epubcheck` after building each EPUB
- `--verbose`: show progress messages
- `--debug`: show detailed debug logging

## Examples

### Download and build a single book

```bash
poetry run oreilly-library 9781718504417
```

### Download multiple books into a custom folder

```bash
poetry run oreilly-library \
  --output-dir ./working/Books \
  9781718504417 9781492056355
```

### Reuse a saved cookies file

```bash
poetry run oreilly-library \
  --cookie-file ./working/cookies.json \
  9781718504417
```

### Force a new interactive login

```bash
poetry run oreilly-library --login 9781718504417
```

### Validate the resulting EPUB with epubcheck

```bash
poetry run oreilly-library --check 9781718504417
```

## Output layout

For each book, the downloader creates a working folder containing metadata and
downloaded assets. A typical directory looks like this:

```text
working/Books/
└── Book Title (9781718504417)/
    ├── 9781718504417.json
    ├── chapters.json
    ├── spine.json
    ├── toc.json
    ├── cover.json
    ├── Images/
    ├── Styles/
    ├── fonts/
    ├── xhtml/
    └── 9781718504417.epub
```

## How it works

The downloader:

1. Fetches book metadata from O'Reilly APIs
2. Downloads spine, TOC, chapter metadata, and related JSON documents
3. Downloads all referenced files such as XHTML, images, CSS, and fonts
4. Normalizes downloaded resources for local EPUB packaging
5. Builds an EPUB archive with `ebooklib`
6. Optionally runs `epubcheck`

The builder performs additional cleanup, including:

- manifest generation
- TOC and spine construction
- resource path rewriting
- cover detection and normalization
- XHTML/CSS cleanup
- optional post-processing for Calibre/legacy OPF compatibility helpers

## Python API

You can also use the package programmatically.

### Build an EPUB from an existing extracted directory

```python
from pathlib import Path

from oreilly_library.epub_builder import EpubBuilder

builder = EpubBuilder(Path("example"), check=False)
epub_path = builder.build_epub()
print(epub_path)
```

### Download and build with an authenticated session

```python
from pathlib import Path

import requests

from oreilly_library.epub_downloader import EpubDownloader

session = requests.Session()
session.cookies.update({
    "your_cookie_name": "your_cookie_value",
})

downloader = EpubDownloader(
    identifier="9781718504417",
    session=session,
    output_dir=Path("working/Books"),
)

result = downloader.download_and_build()
print(result.epub_path)
```

## Development

Install dependencies:

```bash
poetry install
```

Useful files:

- `src/oreilly_library/__main__.py`: CLI entry point and cookie handling
- `src/oreilly_library/epub_downloader.py`: download workflow and asset storage
- `src/oreilly_library/epub_builder.py`: EPUB assembly and normalization logic

## Notes and limitations

- Selenium login currently supports **Chrome only**
- `--check` requires `epubcheck` (or `epubchecker`) to be installed separately
- Output quality depends on the structure and completeness of O'Reilly source
  assets
- Some books may require additional cleanup if upstream content is malformed

## License / usage note

This repository does not grant rights to O'Reilly content. Make sure your use of
downloaded material complies with O'Reilly's terms and any applicable licensing
restrictions.