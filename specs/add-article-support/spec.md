# Add ARTICLE Download Support

## Objective

Allow the existing EPUB download and build workflow to download O'Reilly
ARTICLE content whose archive resources are exposed through the same EPUB API
family as BOOK content.

The initial acceptance case is `53863MIT59458` (Perfecting Your Thinking
Skills), whose metadata identifies the archive as
`urn:orm:article:53863MIT59458`.

## Requirements

- The downloader MUST fetch generic metadata before constructing archive,
  chapter, asset, or fallback-cover URLs. It MUST resolve `archive_urn` once
  from the generic metadata result, before merging search or archive payloads.
- When the generic metadata result contains an `ourn` value that exactly
  matches either
  `urn:orm:book:<identifier>` or `urn:orm:article:<identifier>` for the
  requested identifier, the downloader MUST retain that exact value as
  `archive_urn`. Archive payloads and search results MUST NOT replace it.
- When the generic metadata result has no `ourn`, a non-string `ourn`, or an
  empty `ourn`, the downloader MUST retain the existing BOOK fallback,
  `urn:orm:book:<identifier>`, so existing inputs continue to work.
- A non-empty `ourn` with leading or trailing whitespace, a different
  identifier, malformed syntax, or an unsupported resource type MUST fail
  before any archive request. The error MUST identify the requested identifier
  and invalid URN; it MUST NOT fall back to BOOK in this case.
- The immutable `archive_urn` MUST be used for the EPUB archive URL,
  `epub-chapters` query value, fallback-cover URL, and files URL. The files URL
  MUST be constructed as `<archive URL>files/?limit=1000`; the implementation
  MUST NOT follow a `files` URL from a merged archive payload.
- Related-document URLs (`spine`, `table_of_contents`, `nav`, and `cover`)
  supplied by the selected archive response MUST remain authoritative and be
  fetched as-is.
- The downloader MUST continue to save the existing working-file layout:
  metadata JSON, spine and table-of-contents JSON when provided, chapter JSON,
  and downloaded files organized by their existing extension-based rules.
- Missing ARTICLE `nav` or `cover` related-document URLs MUST remain
  non-fatal. Existing fallback cover handling remains responsible for a cover
  when needed.
- XHTML and CSS normalization MUST rewrite local asset references for both
  `urn:orm:book:<identifier>/files/` and
  `urn:orm:article:<identifier>/files/` URLs, including absolute and
  root-relative API URLs.
- The EPUB builder MUST preserve the downloaded ARTICLE's `identifier`,
  `title`, `publication_date`, author contributors from
  `talent.contributors`, publishers from `publishers`, and subjects from
  dict-shaped `topics[*].name` when those fields are supplied by the existing
  metadata sources.
- Metadata merged from the generic metadata endpoint and the archive endpoint
  MUST retain generic metadata fields that the archive response omits, such as
  contributors, publishers, and topics.
- A missing archive resource MUST report the actual failing URL and HTTP
  status. The implementation MUST NOT silently reinterpret a 404 response as
  an access failure or fabricate a BOOK archive for an ARTICLE identifier.

## Non-goals

- Add support for content types other than BOOK and ARTICLE.
- Change CLI arguments, cookie collection, Calibre import, or early-release
  tracking behavior.
- Infer an ARTICLE archive solely from the identifier format.
- Accept a URN for any content type other than BOOK or ARTICLE.
- Download content without the user's authorized O'Reilly session.

## Implementation Scope

- `src/oreilly_library/epub_downloader.py`
  - Resolve and retain one `archive_urn` after generic metadata is read.
  - Replace BOOK-specific archive and chapter URL construction with the
    resolved URN.
  - Derive the files and fallback-cover URLs from the resolved URN.
  - Extend downloaded XHTML URL normalization to recognize ARTICLE file URLs.
- `src/oreilly_library/epub_builder.py`
  - Extend its cover, XHTML, and CSS resource URL normalization to recognize
    ARTICLE file URLs alongside BOOK file URLs.

The implementation should add only the `archive_urn` state needed to preserve
this invariant. It should not add a separate ARTICLE downloader, builder path,
content-type hierarchy, or URL-validation subsystem: ARTICLE archive responses
use the existing spine, TOC, chapter, and file response shapes.

## Validation

- Add standard-library `unittest` coverage for URL selection with BOOK
  metadata, ARTICLE metadata, metadata without `ourn`, and invalid `ourn`
  values. Invalid values MUST cover leading/trailing whitespace, a different
  identifier, and an unsupported resource type. Assert that invalid metadata
  makes no `/epubs/` request.
- In one mocked ARTICLE flow, assert exactly one archive request to
  `/epubs/urn:orm:article:<identifier>/`, exactly one files request, and
  exactly one chapters request. Assert each uses the selected ARTICLE URN, no
  requested URL uses `urn:orm:book:<identifier>`, and a conflicting archive
  response `ourn` cannot change subsequent requests.
- Mock the generic metadata and archive responses separately. Assert the
  merged ARTICLE metadata retains generic contributors, publishers, and topics
  when the archive response omits them.
- Unit-test downloader XHTML normalization and builder XHTML/CSS normalization
  with both absolute and root-relative BOOK and ARTICLE file URLs; assert the
  resulting paths are local relative references and do not retain API file
  URLs.
- Use synthetic fixture payloads shaped like the ARTICLE archive for
  `53863MIT59458`: a spine, table of contents, nine chapter records, and file
  entries for HTML, CSS, images, fonts, OPF, NCX, and XPGT resources. Fixtures
  MUST NOT contain downloaded O'Reilly chapter text or binary assets. Verify
  the existing build path produces an EPUB containing the expected manifest and
  spine resources. Inspect the generated OPF and assert its identifier, title,
  publication date, author, publisher, and subjects use the accepted metadata
  field shapes.
- Unit-test an ARTICLE archive with absent `nav` and `cover` fields: neither
  related document is requested, and a downloaded `cover.html` permits a
  successful build.
- Unit-test that a 404 archive response raises `requests.HTTPError` with a
  response status of 404 and the selected archive URL. It must occur before
  chapters or files requests, and must not become a BOOK fallback or an access
  error.
- With an authorized session, run one manual end-to-end download and build for
  `53863MIT59458`; verify the output EPUB opens and its chapter and stylesheet
  references do not point to O'Reilly API file URLs.
- Run the repository's focused test command(s) for the affected tests and
  `poetry run oreilly-library --help`.
