---
name: add-new-books
description: Import newly downloaded O'Reilly EPUBs from a repository's `working/Books` directory into `~/Calibre Library`, preferring `_polished.epub` files. Use when the user asks to "add new books", import books into Calibre, or clean up successfully imported book folders. Detect duplicate titles and overwrite only an existing EPUB marked `early access` or `early release`; remove only source folders with confirmed successful imports.
---

# Add New Books

Import the preferred EPUB for every downloaded book safely, preserving
established Calibre records unless their existing EPUB is an early-access edition.

## Workflow

1. Read the repository's `AGENTS.md` and `AGENTS.local.md` when present. Work
   from the repository root.
2. List EPUBs under `working/Books`. For each book folder, select its
   `_polished.epub` when present; otherwise select its other EPUB. Never import
   both variants.
3. Query `calibredb` against `~/Calibre Library` for the selected book title
   before importing. Use the absolute executable when necessary:

   ```sh
   /Applications/calibre.app/Contents/MacOS/calibredb \
     --with-library "$HOME/Calibre Library" \
     list --for-machine --fields title,tags,comments,formats \
     --search 'title:"Exact Title"'
   ```

4. Import based on the query result:

   - No matching record: run `calibredb add` for the selected EPUB.
   - Matching record marked `early access` or `early release` in its tags or
     comments: run `calibredb add --automerge=overwrite` for the selected EPUB
     to replace the existing EPUB while retaining its record.
   - Any other matching record: do not overwrite it. Leave its source folder in
     place and report it as a non-early-access duplicate.

5. Treat a folder as successful only when `calibredb` reports its book ID as
   added or merged. Do not infer success from a lack of errors.
6. Delete only the top-level `working/Books/<book folder>` directories for
   successful additions or early-access replacements. Preserve folders for
   skipped duplicates and failures.
7. List the remaining directories in `working/Books` and report added,
   replaced, skipped, failed, and removed folders.

## Safety

- Do not delete a source folder until its individual import or merge is confirmed.
- Do not use `--duplicates`; it creates a second record instead of applying the
  requested early-access replacement policy.
- If Calibre reports a database lock, ask the user to close Calibre and retry;
  do not delete any folders.
- Treat `early access` and `early release` case-insensitively in tags and comments.
