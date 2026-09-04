# Prune Early-Release Duplicates

## Objective

Provide a local, opt-in maintenance script that removes incomplete duplicate
Calibre records from a library folder.

## Requirements

- The script MUST accept a Calibre library directory containing `metadata.db`.
- It MUST select only duplicate records with the same normalized title and ISBN.
- It MUST read the EPUB spine, normalize visible XHTML text, and remove a
  record only when its chapter and paragraph fingerprints are a strict subset
  of its paired duplicate's set; ambiguous comparisons stay untouched.
- `--apply` MUST back up `metadata.db`, move selected book folders into a
  library-local quarantine directory, then delete their Calibre records.
- It MUST refuse to modify the database if a selected folder is missing or
  escapes the specified library directory.

## Validation

- Unit tests cover selection, exclusion of a different ISBN, spine-aware text
  comparison, quarantining the incomplete folder, and deletion of only that
  record.
- The script's `--help` command runs successfully.
