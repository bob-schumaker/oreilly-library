from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase
from zipfile import ZipFile

from requests import HTTPError

from oreilly_library.epub_builder import EpubBuilder
from oreilly_library.epub_downloader import EpubDownloader


class FakeResponse:
    def __init__(
        self,
        *,
        payload: object | None = None,
        content: bytes = b"",
        status_code: int = 200,
        url: str,
    ) -> None:
        self._payload = payload
        self.content = content
        self.status_code = status_code
        self.url = url

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = HTTPError(f"{self.status_code} error for {self.url}")
            error.response = self
            raise error


class FakeSession:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.urls.append(url)
        return self.responses[url]


class ArticleDownloadTests(TestCase):
    identifier = "53863MIT59458"

    @property
    def article_urn(self) -> str:
        return f"urn:orm:article:{self.identifier}"

    @property
    def article_url(self) -> str:
        return f"https://learning.oreilly.com/api/v2/epubs/{self.article_urn}/"

    def _generic_metadata(self, *, ourn: object | None = None) -> dict[str, object]:
        return {
            "identifier": self.identifier,
            "ourn": self.article_urn if ourn is None else ourn,
            "name": "Synthetic Article",
            "publication_date": "2018-07-01",
            "talent": {
                "contributors": [
                    {"name": "Example Author", "contributor_type": "author"}
                ]
            },
            "publishers": [{"name": "Example Publisher"}],
            "topics": [{"name": "Critical Thinking"}],
        }

    def _responses(
        self,
        generic_metadata: dict[str, object],
        *,
        archive_status: int = 200,
    ) -> dict[str, FakeResponse]:
        files_url = f"{self.article_url}files/?limit=1000"
        chapters_url = (
            "https://learning.oreilly.com/api/v2/epub-chapters/"
            f"?epub_identifier={self.article_urn}"
        )
        spine_url = f"{self.article_url}spine/"
        toc_url = f"{self.article_url}table-of-contents/"
        cover_url = f"{self.article_url}files/cover.html"
        responses = {
            (
                "https://learning.oreilly.com/search/"
                f"?q={self.identifier}&type=book&rows=100&language=en"
            ): FakeResponse(content=b"", url="html-search"),
            (
                "https://learning.oreilly.com/search/api/search/"
                f"?q={self.identifier}&type=book&rows=100&language=en"
            ): FakeResponse(payload={"data": {"products": []}}, url="search"),
            (
                "https://learning.oreilly.com/api/v2/metadata/"
                f"?identifier={self.identifier}"
            ): FakeResponse(
                payload={"results": [generic_metadata]}, url="generic-metadata"
            ),
            self.article_url: FakeResponse(
                payload={
                    "identifier": self.identifier,
                    "ourn": f"urn:orm:book:{self.identifier}",
                    "title": "Synthetic Article",
                    "spine": spine_url,
                    "table_of_contents": toc_url,
                    "files": f"https://invalid.example/{self.identifier}/files/",
                },
                status_code=archive_status,
                url=self.article_url,
            ),
            spine_url: FakeResponse(
                payload={
                    "count": 1,
                    "next": None,
                    "results": [
                        {
                            "reference_id": f"{self.identifier}-/cover.html",
                            "title": "Cover",
                        }
                    ],
                },
                url=spine_url,
            ),
            toc_url: FakeResponse(payload={"results": []}, url=toc_url),
            chapters_url: FakeResponse(
                payload={"count": 1, "next": None, "results": []}, url=chapters_url
            ),
            files_url: FakeResponse(
                payload={
                    "count": 1,
                    "next": None,
                    "results": [{"filename": "cover.html", "url": cover_url}],
                },
                url=files_url,
            ),
            cover_url: FakeResponse(
                content=b"<html><body>Cover</body></html>", url=cover_url
            ),
        }
        return responses

    def test_article_download_uses_one_immutable_archive_urn(self) -> None:
        session = FakeSession(self._responses(self._generic_metadata()))
        with tempfile.TemporaryDirectory() as directory:
            downloader = EpubDownloader(self.identifier, session, output_dir=directory)
            source_dir = downloader.download()
            self.assertTrue((source_dir / "xhtml" / "cover.html").is_file())

        files_url = f"{self.article_url}files/?limit=1000"
        chapters_url = (
            "https://learning.oreilly.com/api/v2/epub-chapters/"
            f"?epub_identifier={self.article_urn}"
        )
        self.assertEqual(session.urls.count(self.article_url), 1)
        self.assertEqual(session.urls.count(files_url), 1)
        self.assertEqual(session.urls.count(chapters_url), 1)
        self.assertTrue(source_dir.name.startswith("Synthetic Article"))
        self.assertEqual(downloader.book_info["ourn"], self.article_urn)
        self.assertEqual(
            downloader.book_info["talent"], self._generic_metadata()["talent"]
        )
        self.assertEqual(
            downloader.book_info["publishers"], self._generic_metadata()["publishers"]
        )
        self.assertTrue(
            all(f"urn:orm:book:{self.identifier}" not in url for url in session.urls)
        )
        self.assertNotIn(
            f"https://invalid.example/{self.identifier}/files/", session.urls
        )
        self.assertNotIn(f"{self.article_url}nav/", session.urls)
        self.assertNotIn(f"{self.article_url}cover/", session.urls)

    def test_missing_ourn_falls_back_to_book(self) -> None:
        downloader = EpubDownloader(self.identifier, FakeSession({}))

        self.assertEqual(
            downloader._resolve_archive_urn({}), f"urn:orm:book:{self.identifier}"
        )

    def test_invalid_ourn_rejects_before_archive_request(self) -> None:
        for invalid_urn in (
            f" {self.article_urn}",
            "urn:orm:article:other-id",
            f"urn:orm:video:{self.identifier}",
        ):
            with self.subTest(invalid_urn=invalid_urn):
                session = FakeSession(
                    self._responses(self._generic_metadata(ourn=invalid_urn))
                )
                downloader = EpubDownloader(self.identifier, session)

                with self.assertRaisesRegex(ValueError, self.identifier):
                    downloader.fetch_bookinfo()

                self.assertFalse(any("/epubs/" in url for url in session.urls))

    def test_archive_404_keeps_response_details_and_stops_download(self) -> None:
        session = FakeSession(
            self._responses(self._generic_metadata(), archive_status=404)
        )
        downloader = EpubDownloader(self.identifier, session)

        with self.assertRaises(HTTPError) as caught:
            downloader.fetch_bookinfo()

        self.assertEqual(caught.exception.response.status_code, 404)
        self.assertEqual(caught.exception.response.url, self.article_url)
        self.assertEqual(
            session.urls,
            [
                downloader._html_search_url,
                downloader._search_url,
                downloader._metadata_url,
                self.article_url,
            ],
        )

    def test_article_urls_are_normalized_for_downloader_and_builder(self) -> None:
        downloader = EpubDownloader(self.identifier, FakeSession({}))
        builder = EpubBuilder(Path.cwd())
        for content_type in ("book", "article"):
            with self.subTest(content_type=content_type):
                urn = f"urn:orm:{content_type}:{self.identifier}"
                downloader_html = downloader._normalize_xhtml(
                    (
                        '<img src="https://learning.oreilly.com/api/v2/epubs/'
                        f'{urn}/files/image.jpg" />'
                    ).encode(),
                    Path("xhtml/chapter.html"),
                ).decode()
                builder_css = builder._normalize_css(
                    (
                        "a { background: url('/api/v2/epubs/"
                        f"{urn}/files/image.jpg'); }}"
                    ).encode(),
                    "Styles/article.css",
                    self.identifier,
                ).decode()
                cover_html = builder._render_cover_xhtml(
                    identifier=self.identifier,
                    document_href="xhtml/cover.xhtml",
                    image_href=(
                        "https://learning.oreilly.com/api/v2/epubs/"
                        f"{urn}/files/image.jpg"
                    ),
                )

                self.assertIn("../image.jpg", downloader_html)
                self.assertNotIn("/api/v2/epubs/", downloader_html)
                self.assertIn("image.jpg", builder_css)
                self.assertNotIn("/api/v2/epubs/", builder_css)
                self.assertNotIn("/api/v2/epubs/", cover_html)

    def test_synthetic_article_build_preserves_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory)
            (source_dir / f"{self.identifier}.json").write_text(
                json.dumps(
                    {
                        **self._generic_metadata(),
                        "title": "Synthetic Article",
                        "descriptions": {"text/html": "<p>Description</p>"},
                    }
                ),
                encoding="utf-8",
            )
            (source_dir / "spine.json").write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "reference_id": (f"{self.identifier}-/{file_name}"),
                                "title": file_name,
                            }
                            for file_name in (
                                "cover.html",
                                *(f"chapter{number:03}.html" for number in range(1, 9)),
                            )
                        ]
                    }
                ),
                encoding="utf-8",
            )
            xhtml_dir = source_dir / "xhtml"
            xhtml_dir.mkdir()
            for file_name in (
                "cover.html",
                *(f"chapter{number:03}.html" for number in range(1, 9)),
            ):
                (xhtml_dir / file_name).write_text(
                    f"<html><body>{file_name}</body></html>", encoding="utf-8"
                )
            styles_dir = source_dir / "Styles"
            styles_dir.mkdir()
            (styles_dir / "article.css").write_text("body {}", encoding="utf-8")
            images_dir = source_dir / "Images"
            images_dir.mkdir()
            (images_dir / "image.jpg").write_bytes(b"synthetic image")
            fonts_dir = source_dir / "fonts"
            fonts_dir.mkdir()
            (fonts_dir / "article.otf").write_bytes(b"synthetic font")
            (source_dir / "article.xpgt").write_bytes(b"synthetic xpgt")

            epub_path = EpubBuilder(source_dir).build_epub()

            self.assertTrue(epub_path.is_file())
            with ZipFile(epub_path) as archive:
                opf = archive.read("OEBPS/content.opf").decode("utf-8")

            self.assertIn(f">{self.identifier}</dc:identifier>", opf)
            self.assertIn("<dc:title>Synthetic Article</dc:title>", opf)
            self.assertIn("<dc:date>2018-07-01</dc:date>", opf)
            self.assertIn("Example Author</dc:creator>", opf)
            self.assertIn("<dc:publisher>Example Publisher</dc:publisher>", opf)
            self.assertIn("<dc:subject>Critical Thinking</dc:subject>", opf)
            self.assertIn('href="xhtml/chapter008.html"', opf)
