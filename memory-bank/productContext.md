# Product Context

## Problem Solved

O'Reilly Learning books expose metadata, chapters, and static assets through web
and API endpoints, but users may want a local EPUB artifact for authorized
offline reading, validation, or archive workflows.

This project automates the repetitive work of collecting those pieces and
assembling them into a usable EPUB.

## Primary Users

- A user with legitimate O'Reilly Learning access who wants local EPUB files for
  books they are authorized to access.
- A developer maintaining download, normalization, and packaging behavior for
  different O'Reilly book structures.

## Expected User Experience

- Run the CLI with one or more ISBN-like identifiers.
- Provide cookies or complete a browser-based login when needed.
- Receive a working directory containing downloaded metadata/assets and an EPUB
  file.
- Rebuild from local files with `--build` without repeating network downloads.
- Use optional validation/cleanup flags when local external tools are present.

## Important Usage Note

The repository README explicitly states that this tool should only be used with
content the user is authorized to access through their own O'Reilly account.
