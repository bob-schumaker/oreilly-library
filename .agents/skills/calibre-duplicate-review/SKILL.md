---
name: calibre-duplicate-review
description: Review possible duplicate EPUBs in a specified Calibre library using deterministic metadata and text comparison, with model judgment only for ambiguous cases. Use when asked to find, classify, or safely remove duplicate Calibre books.
---

# Calibre Duplicate Review

Review one specified Calibre library directory. The live source of truth is its
`metadata.db` and its book folders; do not require or alter a copied database.

## Deterministic pass

1. Create a read-only report from the live library:

   ```sh
   poetry run python bin/calibre_duplicate_discover.py \
     "<library-folder>" --output duplicate-report.json
   ```

2. Treat records with the same normalized title and ISBN as one duplicate
   category, regardless of early-release tags. Titles denoting different
   editions are separate books, not duplicates.
3. The checker may select a pair only when normalized visible text from its EPUB
   spine is a strict subset of the other copy's text. It intentionally leaves
   equal, rewritten, malformed, image-only, and three-or-more-copy groups alone.

## Ambiguous-review pass

Use model judgment only after narrowing to an individual candidate group. Supply
the minimum relevant evidence: IDs, title, author, identifiers, publication and
import dates, tags, EPUB sizes, and a compact chapter/paragraph text-difference
summary. Do not provide complete EPUB archives or whole libraries.

Classify each ambiguous group as one of:

- **distinct edition** — retain both;
- **newer revision** — retain both unless the user asks to remove superseded
  releases;
- **likely duplicate** — present a proposed keeper and metadata conflicts for
  user approval;
- **insufficient evidence** — retain both and explain what evidence is missing.

Do not let a model classification trigger deletion by itself. Model reasoning is
review evidence, not a destructive-action authorization.

## Mutation and recovery

After the user approves exact candidate IDs, create `decisions.json` with a
`decisions` array of `{ "remove_id": 123, "keep_id": 456 }` objects and run
the apply command with `--apply`. It refuses stale report relationships, creates
a `metadata.db` backup, and quarantines removed folders under
`.oreilly-duplicate-quarantine/` before deleting Calibre records. Report the
backup and quarantine paths after completion.

Do not merge conflicting metadata automatically. Present author, tag, comment,
publisher, series, and identifier conflicts for approval before editing a
retained record.
