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
EPUB_SEARCH_URL = (
    "https://learning.oreilly.com/search/api/search/"
    "?q={identifier}&type=article&type=book&type=shortcut&rows=100&language=en&language=ja&feature_flags=improveSearchFilters&tzOffset=8&aia_only=false&report=true&isTopics=false"
)

COVER_MIN_WIDTH = 510
COVER_MIN_HEIGHT = 680
COVER_FALLBACK_URL = "https://learning.oreilly.com/covers/{book_urn}/{size}{axis}/"


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
        self._log_info("Ensuring cover assets")
        self._ensure_cover_assets(book_info)

        self._log_info("Completed download for %s", self.identifier)

        return self.destination

    def download_and_build(self) -> DownloadResult:
        """Download assets and immediately build an EPUB archive."""
        source_dir = self.download()
        self._log_info("Building EPUB archive for %s", self.identifier)
        self._log_debug("Creating EpubBuilder for %s", source_dir)
        builder = EpubBuilder(
            source_dir,
            verbose=self._verbose,
            debug=self._debug,
        )
        epub_path = builder.build_epub()
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

    @property
    def _search_url(self) -> str:
        return EPUB_SEARCH_URL.format(identifier=self.identifier)

    def fetch_bookinfo(self) -> MutableMapping[str, Any]:
        """Get the book metadata."""
        book_info = {}
        search_info = self._fetch_json(self._search_url)
        if search_info and "data" in search_info:
            products = search_info["data"].get("products")
            if products:
                book_info.update(products[0])
        api_info = self._fetch_json(self._main_api_url)
        if api_info:
            book_info.update(api_info)
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
            if target_path.exists() and self._related_document_is_complete(target_path):
                self._log_debug("Skipping existing %s document at %s", key, target_path)
                continue
            url = metadata.get(key)
            if isinstance(url, str):
                self._log_debug("Fetching %s document from %s", key, url)
                data = self._fetch_related_document(url)
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

    def _fetch_related_document(self, url: str) -> object:
        first_page = self._fetch_json(url)
        if not self._is_paginated_payload(first_page):
            return first_page

        aggregated: MutableMapping[str, Any] = dict(first_page)
        results_data = first_page.get("results")
        aggregated["results"] = (
            list(results_data) if isinstance(results_data, list) else []
        )

        next_value = first_page.get("next")
        next_url = next_value if isinstance(next_value, str) else None
        while next_url:
            page = self._fetch_json(next_url)
            new_items = page.get("results")
            if isinstance(new_items, list):
                aggregated["results"].extend(new_items)
            next_value = page.get("next")
            next_url = next_value if isinstance(next_value, str) else None

        aggregated["next"] = None
        aggregated["previous"] = None

        count = aggregated.get("count")
        result_count = len(aggregated["results"])
        aggregated["count"] = (
            max(count, result_count) if isinstance(count, int) else result_count
        )
        return aggregated

    def _related_document_is_complete(self, path: Path) -> bool:
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except OSError, json.JSONDecodeError:
            return False

        if not self._is_paginated_payload(payload):
            return True

        results = payload.get("results")
        if not isinstance(results, list):
            return False

        next_value = payload.get("next")
        if isinstance(next_value, str) and next_value:
            return False

        count = payload.get("count")
        if isinstance(count, int) and len(results) < count:
            return False

        return True

    def _is_paginated_payload(self, payload: object) -> bool:
        return (
            isinstance(payload, Mapping)
            and isinstance(payload.get("results"), list)
            and (
                isinstance(payload.get("count"), int)
                or "next" in payload
                or "previous" in payload
            )
        )

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

    def _ensure_cover_assets(self, metadata: Mapping[str, Any]) -> None:
        cover_document_path = self._local_cover_document_path()
        cover_image_path = self._local_cover_image_path()
        cover_dimensions: Optional[tuple[int, int]] = None

        if cover_image_path is None:
            cover_image_path, cover_dimensions = self._download_cover_image_fallback(
                metadata
            )
        else:
            try:
                cover_dimensions = self._image_dimensions_from_bytes(
                    cover_image_path.read_bytes()
                )
            except OSError:
                cover_dimensions = None

        if cover_image_path is None:
            return

        if cover_document_path is None:
            self._write_cover_document(cover_image_path, cover_dimensions)
            self._prepend_cover_to_spine()

    def _download_cover_image_fallback(
        self,
        metadata: Mapping[str, Any],
    ) -> tuple[Optional[Path], Optional[tuple[int, int]]]:
        cover_hint = self._extract_cover_image_hint(metadata)
        if not cover_hint:
            self._log_debug("No cover_image metadata available for %s", self.identifier)
            return None, None

        hint_dimensions: Optional[tuple[int, int]] = None
        try:
            hint_bytes = self._fetch_bytes(cover_hint)
            hint_dimensions = self._image_dimensions_from_bytes(hint_bytes)
        except Exception as exc:  # pragma: no cover - network failure path
            self._log_debug("Unable to inspect cover_image %s: %s", cover_hint, exc)

        request_size, request_axis = self._cover_request_target(hint_dimensions)
        book_urn = self._book_urn(metadata)
        candidate_targets = [(request_size, request_axis)]
        alternate_target = (
            (COVER_MIN_HEIGHT, "h") if request_axis == "w" else (COVER_MIN_WIDTH, "w")
        )
        if alternate_target != candidate_targets[0]:
            candidate_targets.append(alternate_target)

        best_candidate: tuple[bytes, Optional[tuple[int, int]], str] | None = None
        best_score: tuple[int, int, int] = (-1, -1, -1)

        for size, axis in candidate_targets:
            url = COVER_FALLBACK_URL.format(book_urn=book_urn, size=size, axis=axis)
            try:
                image_bytes = self._fetch_bytes(url)
            except Exception as exc:  # pragma: no cover - network failure path
                self._log_debug("Unable to fetch fallback cover image %s: %s", url, exc)
                continue

            dimensions = self._image_dimensions_from_bytes(image_bytes)
            score = self._cover_candidate_score(dimensions)
            if best_candidate is None or score > best_score:
                best_candidate = (image_bytes, dimensions, url)
                best_score = score

            if self._cover_meets_minimums(dimensions):
                break

        if best_candidate is None:
            return None, None

        image_bytes, dimensions, source_url = best_candidate
        extension = self._image_extension_from_bytes(image_bytes)
        target_path = self.destination / "Images" / f"cover{extension}"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_debug(
            "Saving fallback cover image from %s to %s", source_url, target_path
        )
        target_path.write_bytes(image_bytes)
        return target_path, dimensions

    def _extract_cover_image_hint(self, metadata: Mapping[str, Any]) -> Optional[str]:
        direct_keys = (
            "cover_image",
            "cover_image_url",
            "cover",
        )
        for key in direct_keys:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        chapters_path = self.destination / "chapters.json"
        if not chapters_path.exists():
            return None

        try:
            with chapters_path.open("r", encoding="utf-8") as handle:
                chapters_payload = json.load(handle)
        except OSError, json.JSONDecodeError:
            return None

        if isinstance(chapters_payload, Mapping):
            results = chapters_payload.get("results")
            if isinstance(results, list):
                for item in results:
                    if not isinstance(item, Mapping):
                        continue
                    value = item.get("cover_image")
                    if isinstance(value, str) and value.strip():
                        return value.strip()

        return None

    def _book_urn(self, metadata: Mapping[str, Any]) -> str:
        urn = metadata.get("ourn")
        if isinstance(urn, str) and urn.strip():
            return urn.strip()
        return f"urn:orm:book:{self.identifier}"

    def _local_cover_document_path(self) -> Optional[Path]:
        xhtml_dir = self.destination / "xhtml"
        for file_name in ("cover.xhtml", "cover.html"):
            candidate = xhtml_dir / file_name
            if candidate.exists():
                return candidate
        return None

    def _local_cover_image_path(self) -> Optional[Path]:
        images_dir = self.destination / "Images"
        if not images_dir.exists():
            return None

        candidate_patterns = (
            "cover.*",
            f"{self.identifier}.*",
            "*cover*.*",
        )
        seen: set[Path] = set()
        for pattern in candidate_patterns:
            for candidate in sorted(images_dir.glob(pattern)):
                if candidate.is_file() and candidate not in seen:
                    seen.add(candidate)
                    return candidate
        return None

    def _write_cover_document(
        self,
        image_path: Path,
        dimensions: Optional[tuple[int, int]],
    ) -> None:
        xhtml_dir = self.destination / "xhtml"
        xhtml_dir.mkdir(parents=True, exist_ok=True)

        cover_document_path = xhtml_dir / "cover.xhtml"
        relative_href = Path("..") / image_path.relative_to(self.destination)
        width_attr = f' width="{dimensions[0]}"' if dimensions else ""
        height_attr = f' height="{dimensions[1]}"' if dimensions else ""

        cover_document = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">\n'
            "<head>\n"
            "<title>Cover</title>\n"
            "</head>\n"
            "<body>\n"
            f'<img src="{relative_href.as_posix()}" alt="Cover image"{width_attr}{height_attr} />\n'
            "</body>\n"
            "</html>\n"
        )
        self._log_debug("Writing synthetic cover document to %s", cover_document_path)
        cover_document_path.write_text(cover_document, encoding="utf-8")

    def _prepend_cover_to_spine(self) -> None:
        spine_path = self.destination / "spine.json"
        if not spine_path.exists():
            return

        try:
            with spine_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except OSError, json.JSONDecodeError:
            return

        if not isinstance(payload, MutableMapping):
            return
        results = payload.get("results")
        if not isinstance(results, list):
            return

        for entry in results:
            if not isinstance(entry, Mapping):
                continue
            reference_id = entry.get("reference_id")
            if (
                isinstance(reference_id, str)
                and Path(reference_id).name == "cover.xhtml"
            ):
                return

        results.insert(
            0,
            {
                "reference_id": f"{self.identifier}-/xhtml/cover.xhtml",
                "title": "Cover",
            },
        )
        count = payload.get("count")
        payload["count"] = (
            max(count, len(results)) if isinstance(count, int) else len(results)
        )
        self._write_json(payload, spine_path)

    def _cover_candidate_score(
        self,
        dimensions: Optional[tuple[int, int]],
    ) -> tuple[int, int, int]:
        if dimensions is None:
            return (0, 0, 0)

        width, height = dimensions
        meets_target = int(self._cover_meets_minimums(dimensions))
        matching_dimensions = int(width >= COVER_MIN_WIDTH) + int(
            height >= COVER_MIN_HEIGHT
        )
        return (meets_target, matching_dimensions, width * height)

    def _cover_meets_minimums(
        self,
        dimensions: Optional[tuple[int, int]],
    ) -> bool:
        if dimensions is None:
            return False
        width, height = dimensions
        return width >= COVER_MIN_WIDTH and height >= COVER_MIN_HEIGHT

    def _cover_request_target(
        self,
        dimensions: Optional[tuple[int, int]],
    ) -> tuple[int, str]:
        if dimensions is None:
            return COVER_MIN_HEIGHT, "h"

        width, height = dimensions
        if width <= 0 or height <= 0:
            return COVER_MIN_HEIGHT, "h"

        scaled_height_at_min_width = (COVER_MIN_WIDTH * height) / width
        if scaled_height_at_min_width >= COVER_MIN_HEIGHT:
            return COVER_MIN_WIDTH, "w"
        return COVER_MIN_HEIGHT, "h"

    def _image_dimensions_from_bytes(
        self,
        image_bytes: bytes,
    ) -> Optional[tuple[int, int]]:
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n") and len(image_bytes) >= 24:
            width = int.from_bytes(image_bytes[16:20], "big")
            height = int.from_bytes(image_bytes[20:24], "big")
            return width, height

        if image_bytes[:6] in {b"GIF87a", b"GIF89a"} and len(image_bytes) >= 10:
            width = int.from_bytes(image_bytes[6:8], "little")
            height = int.from_bytes(image_bytes[8:10], "little")
            return width, height

        if image_bytes.startswith(b"\xff\xd8"):
            offset = 2
            sof_markers = {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }
            while offset < len(image_bytes):
                while offset < len(image_bytes) and image_bytes[offset] != 0xFF:
                    offset += 1
                while offset < len(image_bytes) and image_bytes[offset] == 0xFF:
                    offset += 1
                if offset >= len(image_bytes):
                    break
                marker = image_bytes[offset]
                offset += 1
                if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                    continue
                if offset + 2 > len(image_bytes):
                    break
                segment_length = int.from_bytes(image_bytes[offset : offset + 2], "big")
                if segment_length < 2 or offset + segment_length > len(image_bytes):
                    break
                if marker in sof_markers and segment_length >= 7:
                    height = int.from_bytes(image_bytes[offset + 3 : offset + 5], "big")
                    width = int.from_bytes(image_bytes[offset + 5 : offset + 7], "big")
                    return width, height
                offset += segment_length

        return None

    def _image_extension_from_bytes(self, image_bytes: bytes) -> str:
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if image_bytes[:6] in {b"GIF87a", b"GIF89a"}:
            return ".gif"
        if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
            return ".webp"
        return ".jpg"

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
