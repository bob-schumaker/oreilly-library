"""Utilities for downloading O'Reilly EPUB assets and building archives."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, MutableMapping, Optional

from requests import Session

from oreilly_library.epub_builder import EpubBuilder

EPUB_METADATA_URL = (
    "https://learning.oreilly.com/api/v2/metadata/?identifier={identifier}"
)
EPUB_API_URL = "https://learning.oreilly.com/api/v2/epubs/urn:orm:book:{identifier}/"
EPUB_CHAPTERS_URL = (
    "https://learning.oreilly.com/api/v2/epub-chapters/"
    "?epub_identifier=urn:orm:book:{identifier}"
)
EPUB_CHAPTERS_URL = (
    "https://learning.oreilly.com/search/api/search/"
    "?q={identifier}&type=article&type=book&type=shortcut&rows=100&language=en&language=ja&feature_flags=improveSearchFilters&tzOffset=8&aia_only=false&report=true&isTopics=false"
)


@dataclass(frozen=True)
class DownloadResult:
    """Information about a completed download operation."""

    source_dir: Path
    epub_path: Optional[Path] = None


class EpubDownloader:
    """Download the assets required to build an EPUB archive.

    Parameters
    ----------
    identifier:
        The O'Reilly identifier (e.g. ``9781837020294``).
    session:
        An authenticated :class:`requests.Session` used for HTTP requests.
    output_dir:
        Base directory where downloaded assets will be stored. Files are
        written to ``{output_dir}/{title (identifier)}`` with resource files
        sorted into ``fonts/``, ``images/``, ``styles/``, or ``text/``
        subfolders.
    """

    def __init__(
        self,
        identifier: str,
        session: Session,
        output_dir: Path | str = Path("data"),
        *,
        verbose: bool = False,
        debug: bool = False,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.identifier = identifier
        self.session = session
        self.base_output_dir = Path(output_dir)
        self.destination = (self.base_output_dir / identifier).resolve()
        self._verbose = bool(verbose) and not bool(debug)
        self._debug = bool(debug)
        self.logger = logger or logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def download(self) -> Path:
        """Download metadata and static assets required for the EPUB.

        Returns
        -------
        Path
            The directory containing the downloaded assets.
        """

        self._log_info("Fetching metadata from %s", self._metadata_url)
        book_info = self.fetch_bookinfo()
        self._ensure_named_destination(book_info)
        metadata_path = self.destination / f"{self.identifier}.json"
        if not metadata_path.exists():
            self._log_debug("Persisting metadata to %s", metadata_path)
            self._write_json(book_info, metadata_path)

        self._log_info("Downloading related documents")
        self._download_related_documents(book_info)
        self._log_info("Aggregating chapter data")
        self._download_chapters()
        self._log_info("Downloading additional files")
        self._download_files(book_info)

        self._log_info("Completed download for %s", self.identifier)

        return self.destination

    def download_and_build(self, calibre: bool = False) -> DownloadResult:
        """Download assets and immediately build an EPUB archive."""
        source_dir = self.download()
        self._log_info("Building EPUB archive for %s", self.identifier)
        self._log_debug("Creating EpubBuilder for %s", source_dir)
        builder = EpubBuilder(source_dir)
        epub_path = builder.build_epub(calibre=calibre)
        self._log_info("Finished building EPUB archive at %s", epub_path)
        return DownloadResult(source_dir=source_dir, epub_path=epub_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @property
    def _main_api_url(self) -> str:
        return EPUB_API_URL.format(identifier=self.identifier)

    @property
    def _metadata_url(self) -> str:
        return EPUB_METADATA_URL.format(identifier=self.identifier)

    @property
    def _chapters_url(self) -> str:
        return EPUB_CHAPTERS_URL.format(identifier=self.identifier)

    def fetch_bookinfo(self) -> MutableMapping[str, Any]:
        """Get the book metadata."""
        book_info = self._fetch_json(self._main_api_url)
        metadata = self._fetch_json(self._metadata_url)
        results = metadata.get("results")
        if results:
            book_info.update(results[0])
        return book_info

    def _download_related_documents(self, metadata: Mapping[str, Any]) -> None:
        related_endpoints = {
            "spine": "spine.json",
            "table_of_contents": "toc.json",
            "nav": "nav.json",
            "cover": "cover.json",
        }

        for key, filename in related_endpoints.items():
            target_path = self.destination / filename
            if target_path.exists():
                self._log_debug("Skipping existing %s document at %s", key, target_path)
                continue
            url = metadata.get(key)
            if isinstance(url, str):
                self._log_debug("Fetching %s document from %s", key, url)
                data = self._fetch_json(url)
                self._write_json(data, target_path)

    def _download_chapters(self) -> None:
        chapters_path = self.destination / "chapters.json"
        if chapters_path.exists():
            self._log_debug(
                "Skipping chapters download; %s already exists", chapters_path
            )
            return
        aggregated: MutableMapping[str, Any] | None = None
        for page in self._iterate_paginated(self._chapters_url):
            self._log_debug("Processing chapters page with keys: %s", list(page.keys()))
            if aggregated is None:
                aggregated = dict(page)
                results_data = page.get("results")
                aggregated["results"] = (
                    list(results_data) if isinstance(results_data, list) else []
                )
            else:
                results = aggregated.setdefault("results", [])
                if isinstance(results, list):
                    new_items = page.get("results")
                    if isinstance(new_items, list):
                        results.extend(new_items)
            next_url = page.get("next")
            self._log_debug("Next chapters page: %s", next_url)
            if not next_url:
                break
        if aggregated is None:
            aggregated = {}
        self._log_debug("Writing aggregated chapters to %s", chapters_path)
        self._write_json(aggregated, chapters_path)

    def _download_files(self, metadata: Mapping[str, Any]) -> None:
        files_url = metadata.get("files")
        if not isinstance(files_url, str):
            files_url = f"{self._main_api_url}files/"

        for page in self._iterate_paginated(files_url):
            self._log_debug("Processing files page from %s", files_url)
            results = page.get("results", []) or []
            if isinstance(results, list):
                for item in results:
                    self._download_file_entry(item)

    def _download_file_entry(self, item: Mapping[str, Any]) -> None:
        path = self._extract_path(item)
        url = self._extract_url(item)
        if not path or not url:
            self._log_debug("Skipping file entry with insufficient data: %s", item)
            return

        target = self._resolve_target_path(path)
        if target.exists():
            self._log_debug("Asset already exists, skipping download: %s", target)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        self._log_debug("Downloading asset from %s to %s", url, target)
        content = self._fetch_bytes(url)
        if target.suffix.lower() in {".xhtml", ".html"}:
            relative_target = target.relative_to(self.destination)
            self._log_debug(
                "Normalizing XHTML content for %s (relative: %s)",
                target,
                relative_target,
            )
            content = self._normalize_xhtml(content, relative_target)
        target.write_bytes(content)

    def _normalize_xhtml(self, content: bytes, relative_target: Path) -> bytes:
        """Ensure downloaded XHTML is well-formed and references local assets."""

        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return content

        text = text.replace("\r\n", "\n")

        stripped = text.lstrip()
        xml_decl = ""
        if stripped.startswith("<?xml"):
            idx = stripped.find("?>")
            if idx != -1:
                xml_decl = stripped[: idx + 2]
                chop = slice(idx + 2, len(stripped))
                stripped = stripped[chop].lstrip()
        else:
            xml_decl = '<?xml version="1.0" encoding="utf-8"?>'

        text_body = stripped

        if not re.search(r"<html\b", text_body, re.IGNORECASE):
            body_content = text_body
            text_body = (
                "<html>\n"
                "<head>\n"
                '<meta charset="utf-8" />\n'
                "<title></title>\n"
                "</head>\n"
                "<body>\n"
                f"{body_content}\n"
                "</body>\n"
                "</html>\n"
            )

        html_pattern = re.compile(r"<html\b([^>]*)>", re.IGNORECASE)

        def _ensure_html_attrs(match: re.Match[str]) -> str:
            attrs = match.group(1)

            def ensure(name: str, value: str) -> None:
                nonlocal attrs
                if re.search(rf"\b{name}\s*=", attrs) is None:
                    attrs += f' {name}="{value}"'

            ensure("xmlns", "http://www.w3.org/1999/xhtml")
            ensure("xmlns:epub", "http://www.idpf.org/2007/ops")
            ensure("xml:lang", "en")
            ensure("lang", "en")
            return f"<html{attrs}>"

        text_body = html_pattern.sub(_ensure_html_attrs, text_body, count=1)

        void_elements = (
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        )
        void_pattern = re.compile(
            rf"<(?P<tag>{'|'.join(void_elements)})(?P<attrs>[^<>]*?)(?<!/)>",
            re.IGNORECASE,
        )

        def _self_close(match: re.Match[str]) -> str:
            full = match.group(0)
            if full.endswith("/>"):
                return full
            tag = match.group("tag")
            attrs = match.group("attrs").rstrip()
            if attrs and not attrs.startswith(" "):
                attrs = " " + attrs.lstrip()
            return f"<{tag}{attrs} />"

        text_body = void_pattern.sub(_self_close, text_body)

        depth = max(len(relative_target.parts) - 1, 0)
        prefix = "../" * depth

        if prefix:
            text_body = text_body.replace("{prefix}//", prefix)

        api_pattern = re.compile(
            rf"https?://[^'\"]*/api/v\d+/epubs/urn:orm:book:{re.escape(self.identifier)}/files/",
            re.IGNORECASE,
        )
        text_body = api_pattern.sub(prefix, text_body)

        api_root_pattern = re.compile(
            rf"/api/v\d+/epubs/urn:orm:book:{re.escape(self.identifier)}/files/",
            re.IGNORECASE,
        )
        text_body = api_root_pattern.sub(prefix, text_body)

        normalized = f"{xml_decl}\n{text_body.lstrip()}"
        return normalized.encode("utf-8")

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    def _fetch_json(self, url: str) -> MutableMapping[str, Any]:
        self._log_debug("GET %s", url)
        response = self.session.get(url)
        response.raise_for_status()
        return response.json()

    def _fetch_bytes(self, url: str) -> bytes:
        self._log_debug("GET %s (bytes)", url)
        response = self.session.get(url)
        response.raise_for_status()
        return response.content

    def _iterate_paginated(self, url: str) -> Iterator[MutableMapping[str, Any]]:
        next_url: Optional[str] = url
        while next_url:
            page = self._fetch_json(next_url)
            yield page
            next_value = page.get("next")
            next_url = next_value if isinstance(next_value, str) else None

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _write_json(self, data: object, path: Path) -> None:
        self._log_debug("Writing JSON data to %s", path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)

    def _extract_path(self, item: Mapping[str, object]) -> Optional[Path]:
        candidates = (
            item.get("path"),
            item.get("relative_path"),
            item.get("name"),
            item.get("filename"),
        )
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return Path(candidate)
        return None

    def _extract_url(self, item: Mapping[str, Any]) -> Optional[str]:
        for key in ("url", "href", "download_url", "content_url"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _resolve_target_path(self, original: Path) -> Path:
        """Determine the local storage path for a downloaded asset."""

        suffix = original.suffix.lower()
        if suffix in {".ttf", ".otf", ".woff", ".woff2", ".eot"}:
            subdir = "fonts"
        elif suffix in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".webp",
            ".bmp",
            ".tiff",
            ".ico",
        }:
            subdir = "Images"
        elif suffix == ".css":
            subdir = "Styles"
        else:
            subdir = "xhtml"

        return self.destination / subdir / original.name

    def _ensure_named_destination(self, metadata: Mapping[str, Any]) -> None:
        title = metadata.get("name")
        if not isinstance(title, str) or not title.strip():
            return

        desired_name = self._sanitize_directory_name(
            f"{title.strip()} ({self.identifier})"
        )
        if not desired_name:
            return

        desired_path = (self.base_output_dir / desired_name).resolve()

        if desired_path == self.destination:
            return

        if not desired_path.exists() and self.destination.exists():
            self.destination.rmdir()
        self.destination = desired_path
        self._log_debug("Download destination resolved to %s", self.destination)

    def _sanitize_directory_name(self, name: str) -> str:
        sanitized = re.sub(r"[\\/:*?\"<>|]", "_", name)
        sanitized = sanitized.strip()
        sanitized = re.sub(r"\s+", " ", sanitized)
        sanitized = sanitized.rstrip(".")
        sanitized = sanitized[:255]
        return sanitized

    def _log_info(self, message: str, *args: object) -> None:
        if self._verbose or self._debug:
            self.logger.info(message, *args)

    def _log_debug(self, message: str, *args: object) -> None:
        if self._debug:
            self.logger.debug(message, *args)


__all__ = ["EpubDownloader", "DownloadResult"]
