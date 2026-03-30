"""Utilities for assembling EPUB archives from local folders with ebooklib.

Usage example
-------------

from pathlib import Path
from oreilly_library.epub_builder import EpubBuilder

builder = EpubBuilder(Path("example"))
epub_path = builder.build_epub()
print(f"EPUB created at {epub_path}")
"""

from __future__ import annotations

import html
import json
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit
from zipfile import ZIP_STORED, ZipFile

from ebooklib import epub

# Ensure key MIME types are registered with the mimetypes module.
mimetypes.init()
mimetypes.add_type("application/xhtml+xml", ".xhtml")
mimetypes.add_type("image/jpeg", ".jpg")
mimetypes.add_type("image/jpeg", ".jpeg")
mimetypes.add_type("image/png", ".png")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("font/ttf", ".ttf")
mimetypes.add_type("application/vnd.ms-opentype", ".otf")
mimetypes.add_type("font/woff", ".woff")
mimetypes.add_type("font/woff2", ".woff2")


@dataclass(slots=True)
class ManifestItem:
    """Representation of an item inside the EPUB manifest."""

    item_id: str
    href: str
    media_type: str
    properties: list[str] = field(default_factory=list)


class EpubBuilder:
    """Create an EPUB archive from a directory tree similar to ``data/``."""

    def __init__(
        self,
        source_dir: Path | str,
        output_dir: Optional[Path | str] = None,
        *,
        check: bool = False,
        verbose: bool = False,
        debug: bool = False,
    ) -> None:
        self.source_dir = Path(source_dir).resolve()
        self.output_dir = (
            Path(output_dir).resolve() if output_dir is not None else self.source_dir
        )
        self._check = bool(check)
        self._verbose = bool(verbose)
        self._debug = bool(debug)
        self.warnings: list[str] = []
        self._manifest_ids: set[str] = set()
        self._resource_href_lookup: dict[str, str] = {}
        self._resource_hrefs_by_name: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def build_epub(self, calibre: bool = False) -> Path:
        """Build an EPUB file and return the resulting path."""

        self.warnings.clear()

        metadata = self._load_metadata()
        opf_metadata = self._load_opf_metadata()

        identifier = self._resolve_identifier(metadata)
        categories = self._extract_categories(metadata)
        title = self._as_non_empty_str(metadata.get("title"))
        if not title:
            title = self._as_non_empty_str(metadata.get("name")) or identifier
        language = self._as_non_empty_str(metadata.get("language")) or "en"
        unique_id = (
            self._as_non_empty_str(metadata.get("opf_unique_identifier_type"))
            or "bookid"
        )
        publication_date = self._as_non_empty_str(metadata.get("publication_date"))
        authors = self._extract_authors(metadata, opf_metadata)
        publishers = self._extract_publishers(metadata, opf_metadata)
        description_html = self._extract_description(metadata)

        spine_items = self._resolve_spine()
        cover_image_href = self._detect_cover_image_href(identifier)
        manifest_items, nav_source_href = self._build_manifest(
            spine_items,
            cover_image_href=cover_image_href,
        )
        self._prepare_resource_lookup(manifest_items)

        book = self._create_book(
            identifier=identifier,
            unique_id=unique_id,
            title=title,
            language=language,
            publication_date=publication_date,
            authors=authors,
            publishers=publishers,
            categories=categories,
            description_html=description_html,
            manifest_items=manifest_items,
            spine_items=spine_items,
            nav_source_href=nav_source_href,
            cover_image_href=cover_image_href,
        )

        output_path = self.output_dir / f"{identifier}.epub"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        epub.write_epub(
            str(output_path),
            book,
            options={
                "raise_exceptions": True,
                "epub2_guide": True,
            },
        )

        if calibre:
            self._patch_opf_version(output_path, version="2.0")

        self._cleanup_output_archive(output_path, calibre=calibre)

        if self._check:
            self._run_epubcheck(output_path)

        for warning in self.warnings:
            print(f"Warning: {warning}")

        return output_path

    # ------------------------------------------------------------------
    # ebooklib construction
    # ------------------------------------------------------------------
    def _create_book(
        self,
        *,
        identifier: str,
        unique_id: str,
        title: str,
        language: str,
        publication_date: Optional[str],
        authors: Sequence[str],
        publishers: Sequence[str],
        categories: Sequence[str],
        description_html: Optional[str],
        manifest_items: Sequence[ManifestItem],
        spine_items: Sequence[Path],
        nav_source_href: Optional[str],
        cover_image_href: Optional[str],
    ) -> epub.EpubBook:
        book = epub.EpubBook()
        book.FOLDER_NAME = "OEBPS"
        book.IDENTIFIER_ID = unique_id
        book.set_identifier(identifier)
        book.set_title(title)
        book.set_language(language)

        if publication_date:
            book.add_metadata("DC", "date", publication_date)
        if description_html:
            book.add_metadata("DC", "description", description_html)

        for index, author in enumerate(authors, start=1):
            uid = "creator" if index == 1 else f"creator_{index}"
            book.add_author(author, file_as=author, role="aut", uid=uid)

        for publisher in publishers:
            book.add_metadata("DC", "publisher", publisher)

        for category in categories:
            book.add_metadata("DC", "subject", category)

        manifest_lookup: dict[str, epub.EpubItem] = {}
        for manifest_item in manifest_items:
            book_item = self._make_book_item(manifest_item, identifier)
            if book_item is None:
                continue
            book.add_item(book_item)
            manifest_lookup[manifest_item.href] = book_item

        if cover_image_href:
            cover_item = manifest_lookup.get(cover_image_href)
            if cover_item is not None:
                book.add_metadata(
                    None,
                    "meta",
                    "",
                    {"name": "cover", "content": cover_item.get_id()},
                )

        nav_item = epub.EpubNav(
            file_name=self._nav_file_name(nav_source_href, manifest_items),
            title="Navigation",
        )
        book.add_item(nav_item)
        book.add_item(epub.EpubNcx())

        book.toc = self._build_toc(manifest_lookup, spine_items)

        spine_ids: list[str] = ["nav"]
        for path in spine_items:
            href = self._href_from_path(path)
            if nav_source_href and href == nav_source_href:
                self._warn("Skipping source navigation document in spine: %s", href)
                continue

            item = manifest_lookup.get(href)
            if item is None:
                self._warn("Spine item %s missing from manifest", href)
                continue
            spine_ids.append(item.get_id())
        book.spine = spine_ids

        cover_doc_href = self._find_cover_document_href(manifest_lookup)
        if cover_doc_href:
            book.guide.append(
                {"type": "cover", "title": "Cover", "href": cover_doc_href}
            )

        return book

    def _make_book_item(
        self,
        manifest_item: ManifestItem,
        identifier: str,
    ) -> epub.EpubItem | None:
        source_path = self._source_path_from_href(manifest_item.href)
        if not source_path.exists():
            self._warn("Manifest includes missing file: %s", manifest_item.href)
            return None

        data = source_path.read_bytes()
        if source_path.suffix.lower() in {".xhtml", ".html"}:
            data = self._normalize_xhtml(data, manifest_item.href, identifier)
        elif source_path.suffix.lower() == ".css":
            data = self._normalize_css(data, manifest_item.href, identifier)

        item = epub.EpubItem(
            uid=manifest_item.item_id,
            file_name=manifest_item.href,
            media_type=manifest_item.media_type,
            content=data,
        )
        if manifest_item.properties:
            item.properties = list(manifest_item.properties)
        return item

    # ------------------------------------------------------------------
    # Metadata and manifest helpers
    # ------------------------------------------------------------------
    def _load_metadata(self) -> dict[str, Any]:
        json_files = list(self.source_dir.glob("*.json"))
        for meta_path in json_files:
            try:
                with meta_path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except Exception as exc:  # pragma: no cover - defensive
                self._warn("Failed to read metadata file %s: %s", meta_path.name, exc)
                continue
            if isinstance(data, dict) and "identifier" in data:
                return data

        if json_files:
            fallback = json_files[0]
            self._warn(
                "No metadata JSON with identifier found; using %s",
                fallback.name,
            )
            try:
                with fallback.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                return data if isinstance(data, dict) else {}
            except Exception as exc:  # pragma: no cover - defensive
                self._warn(
                    "Failed to parse fallback metadata file %s: %s",
                    fallback.name,
                    exc,
                )

        self._warn("No metadata JSON files found in %s", self.source_dir)
        return {}

    def _resolve_identifier(self, metadata: Mapping[str, Any]) -> str:
        identifier = self._as_non_empty_str(metadata.get("identifier"))
        if not identifier:
            identifier = self._as_non_empty_str(metadata.get("isbn"))
        if identifier:
            return identifier

        identifier = self.source_dir.name
        self._warn(
            "Metadata missing identifier; using directory name %s",
            identifier,
        )
        return identifier

    def _extract_categories(self, metadata: Mapping[str, Any]) -> list[str]:
        categories: list[str] = []
        raw_categories = metadata.get("categories")
        if isinstance(raw_categories, list):
            for value in raw_categories:
                if isinstance(value, str):
                    categories.append(value)
                elif isinstance(value, list):
                    categories.extend(
                        item for item in value if isinstance(item, str) and item.strip()
                    )

        raw_topics = metadata.get("topics")
        if isinstance(raw_topics, list):
            for topic in raw_topics:
                if isinstance(topic, dict):
                    name = self._as_non_empty_str(topic.get("name"))
                    if name:
                        categories.append(name)

        if metadata.get("roughcut"):
            categories.append("early release")
        return self._unique_strings(categories)

    def _extract_description(self, metadata: Mapping[str, Any]) -> Optional[str]:
        for key in ("descriptions", "description"):
            value = metadata.get(key)
            if isinstance(value, dict):
                description = self._as_non_empty_str(value.get("text/html"))
                if description:
                    return description
            elif isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _extract_authors(
        self,
        metadata: Mapping[str, Any],
        opf_metadata: Mapping[str, list[str]],
    ) -> list[str]:
        authors: list[str] = []

        raw_authors = metadata.get("authors")
        if isinstance(raw_authors, list):
            authors.extend(item for item in raw_authors if isinstance(item, str))
        elif isinstance(raw_authors, str):
            authors.append(raw_authors)

        talent = metadata.get("talent")
        if isinstance(talent, dict):
            contributors = talent.get("contributors")
            if isinstance(contributors, list):
                for contributor in contributors:
                    if not isinstance(contributor, dict):
                        continue
                    if contributor.get("contributor_type") != "author":
                        continue
                    name = self._as_non_empty_str(contributor.get("name"))
                    if name:
                        authors.append(name)

        authors.extend(opf_metadata.get("authors", []))
        return self._unique_strings(authors)

    def _extract_publishers(
        self,
        metadata: Mapping[str, Any],
        opf_metadata: Mapping[str, list[str]],
    ) -> list[str]:
        publishers: list[str] = []

        raw_publishers = metadata.get("publishers")
        if isinstance(raw_publishers, list):
            for publisher in raw_publishers:
                if isinstance(publisher, str):
                    publishers.append(publisher)
                elif isinstance(publisher, dict):
                    name = self._as_non_empty_str(publisher.get("name"))
                    if name:
                        publishers.append(name)

        publishers.extend(opf_metadata.get("publishers", []))
        return self._unique_strings(publishers)

    def _load_opf_metadata(self) -> dict[str, list[str]]:
        """Extract author and publisher details from any OPF file present."""

        authors: list[str] = []
        publishers: list[str] = []
        namespace = {"dc": "http://purl.org/dc/elements/1.1/"}

        for opf_path in sorted(self.source_dir.rglob("*.opf")):
            try:
                tree = ET.parse(opf_path)
                root = tree.getroot()
            except ET.ParseError as exc:
                self._warn("Failed to parse OPF file %s: %s", opf_path, exc)
                continue

            for creator_el in root.findall(".//dc:creator", namespace):
                if creator_el.text:
                    text = creator_el.text.strip()
                    if text and text not in authors:
                        authors.append(text)

            for publisher_el in root.findall(".//dc:publisher", namespace):
                if publisher_el.text:
                    text = publisher_el.text.strip()
                    if text and text not in publishers:
                        publishers.append(text)

            if authors or publishers:
                break

        return {"authors": authors, "publishers": publishers}

    def _resolve_spine(self) -> list[Path]:
        spine_path = self.source_dir / "spine.json"
        text_dir = self._find_optional_dir("xhtml", "text")
        if text_dir is None:
            text_dir = self.source_dir / "xhtml"
        if spine_path.exists():
            try:
                with spine_path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                results = data.get("results") or []
                resolved: list[Path] = []
                for entry in results:
                    if not isinstance(entry, dict):
                        continue
                    ref_id = entry.get("reference_id")
                    if not isinstance(ref_id, str) or not ref_id:
                        continue
                    filename = ref_id.split("/")[-1]
                    candidate = text_dir / filename
                    if candidate.exists():
                        resolved.append(candidate)
                    else:
                        self._warn("Spine references missing file: %s", filename)
                if resolved:
                    resolved_set = {path.resolve() for path in resolved}
                    for html_ext in ("html", "xhtml"):
                        for path in sorted(text_dir.glob(f"*.{html_ext}")):
                            candidate = path.resolve()
                            if candidate not in resolved_set:
                                resolved.append(path)
                                resolved_set.add(candidate)
                    return resolved
            except Exception as exc:  # pragma: no cover - defensive
                self._warn("Unable to parse spine.json: %s", exc)

        if not text_dir.exists():
            self._warn("Missing text directory at %s", text_dir)
            return []

        files = sorted(
            path for pattern in ("*.xhtml", "*.html") for path in text_dir.glob(pattern)
        )
        if not files:
            self._warn("No HTML or XHTML files found in %s", text_dir)
        return files

    def _build_manifest(
        self,
        spine_items: Sequence[Path],
        *,
        cover_image_href: Optional[str],
    ) -> tuple[list[ManifestItem], Optional[str]]:
        manifest: list[ManifestItem] = []
        self._manifest_ids.clear()
        nav_source_href: Optional[str] = None

        def register(
            href: str, path: Path, properties: Optional[Sequence[str]] = None
        ) -> None:
            nonlocal nav_source_href

            if (
                path.suffix.lower() in {".xhtml", ".html"}
                and path.stem.lower() == "nav"
            ):
                nav_source_href = href
                return

            item_id = self._manifest_id_from_href(href)
            media_type = self._guess_media_type(path)
            item_properties = list(properties or [])
            if path.suffix.lower() in {".xhtml", ".html"}:
                item_properties.extend(self._detect_document_properties(path))
            if cover_image_href and href == cover_image_href:
                item_properties.append("cover-image")

            manifest.append(
                ManifestItem(
                    item_id=item_id,
                    href=href,
                    media_type=media_type,
                    properties=item_properties,
                )
            )
            self._manifest_ids.add(item_id)

        text_dir = self._find_optional_dir("xhtml", "text")
        if text_dir is not None:
            for html_ext in ("html", "xhtml"):
                for path in sorted(text_dir.glob(f"*.{html_ext}")):
                    register(self._href_from_path(path), path)
        else:
            self._warn("Missing text directory at %s", self.source_dir / "xhtml")

        styles_dir = self._find_optional_dir("Styles", "styles")
        if styles_dir is not None:
            for path in sorted(styles_dir.glob("*")):
                if path.is_file():
                    register(self._href_from_path(path), path)
        else:
            self._warn("Missing styles directory at %s", self.source_dir / "Styles")

        fonts_dir = self._find_optional_dir("fonts", "Fonts")
        if fonts_dir is not None:
            for path in sorted(fonts_dir.glob("*")):
                if path.is_file():
                    register(self._href_from_path(path), path)

        images_dir = self._find_optional_dir("Images", "images")
        if images_dir is not None:
            for path in sorted(images_dir.glob("*")):
                if path.is_file():
                    register(self._href_from_path(path), path)
        else:
            self._warn("Missing images directory at %s", self.source_dir / "Images")

        manifest_hrefs = {item.href for item in manifest}
        for path in spine_items:
            href = self._href_from_path(path)
            if href not in manifest_hrefs and path.stem.lower() != "nav":
                register(href, path)
                manifest_hrefs.add(href)

        return manifest, nav_source_href

    def _detect_document_properties(self, path: Path) -> list[str]:
        """Detect EPUB manifest properties needed for an XHTML/HTML document."""

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return []

        properties: list[str] = []
        if re.search(
            r"<math\b|http://www\.w3\.org/1998/Math/MathML", text, re.IGNORECASE
        ):
            properties.append("mathml")
        if re.search(r"<svg\b|http://www\.w3\.org/2000/svg", text, re.IGNORECASE):
            properties.append("svg")
        return properties

    def _prepare_resource_lookup(
        self,
        manifest_items: Sequence[ManifestItem],
    ) -> None:
        self._resource_href_lookup.clear()
        self._resource_hrefs_by_name.clear()

        for manifest_item in manifest_items:
            href = manifest_item.href
            self._resource_href_lookup[href.lower()] = href
            file_name = PurePosixPath(href).name.lower()
            if file_name:
                self._resource_hrefs_by_name.setdefault(file_name, []).append(href)

    # ------------------------------------------------------------------
    # TOC helpers
    # ------------------------------------------------------------------
    def _build_toc(
        self,
        manifest_lookup: Mapping[str, epub.EpubItem],
        spine_items: Sequence[Path],
    ) -> tuple[Any, ...]:
        spine_hrefs = [
            href
            for path in spine_items
            if (href := self._href_from_path(path)) in manifest_lookup
            and Path(href).stem.lower() != "nav"
        ]

        raw_toc = self._load_toc_entries()
        if raw_toc:
            built_entries: list[tuple[Optional[str], Any]] = []
            nodes_by_href: dict[str, list[Any]] = {}

            for entry in raw_toc:
                href = self._resolve_toc_href(entry, manifest_lookup)
                node = self._build_toc_node(
                    entry,
                    manifest_lookup,
                    resolved_href=href,
                )
                if node is not None:
                    base_href = href.split("#", 1)[0] if href else None
                    built_entries.append((base_href, node))
                    if base_href:
                        nodes_by_href.setdefault(base_href, []).append(node)

            built: list[Any] = []
            emitted_hrefs: set[str] = set()
            for href in spine_hrefs:
                if href in emitted_hrefs:
                    continue
                emitted_hrefs.add(href)

                nodes = nodes_by_href.get(href)
                if nodes:
                    built.extend(nodes)
                    continue

                built.append(
                    epub.Link(href, self._title_from_href(href), self._toc_uid(href))
                )

            for base_href, node in built_entries:
                if base_href is None or base_href not in emitted_hrefs:
                    built.append(node)

            if built:
                return tuple(built)

        fallback: list[epub.Link] = []
        for href in spine_hrefs:
            fallback.append(
                epub.Link(href, self._title_from_href(href), self._toc_uid(href))
            )
        return tuple(fallback)

    def _load_toc_entries(self) -> list[Mapping[str, Any]]:
        toc_path = self.source_dir / "toc.json"
        if not toc_path.exists():
            return []

        try:
            with toc_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as exc:  # pragma: no cover - defensive
            self._warn("Unable to parse toc.json: %s", exc)
            return []

        if isinstance(data, list):
            return [entry for entry in data if isinstance(entry, dict)]
        if isinstance(data, dict):
            results = data.get("results")
            if isinstance(results, list):
                return [entry for entry in results if isinstance(entry, dict)]
        return []

    def _build_toc_node(
        self,
        entry: Mapping[str, Any],
        manifest_lookup: Mapping[str, epub.EpubItem],
        *,
        resolved_href: Optional[str] = None,
    ) -> Any | None:
        href = resolved_href or self._resolve_toc_href(entry, manifest_lookup)
        title = self._as_non_empty_str(entry.get("title"))
        if not title:
            title = self._title_from_href(href or "Document")

        children_data = entry.get("children")
        children: list[Any] = []
        if isinstance(children_data, list):
            for child in children_data:
                if not isinstance(child, dict):
                    continue
                node = self._build_toc_node(child, manifest_lookup)
                if node is not None:
                    children.append(node)

        if children:
            return (epub.Section(title, href or ""), tuple(children))
        if href:
            return epub.Link(href, title, self._toc_uid(href))
        return None

    def _resolve_toc_href(
        self,
        entry: Mapping[str, Any],
        manifest_lookup: Mapping[str, epub.EpubItem],
    ) -> Optional[str]:
        reference_id = self._as_non_empty_str(entry.get("reference_id"))
        if not reference_id:
            return None

        raw_path = (
            reference_id.split("-/", 1)[1] if "-/" in reference_id else reference_id
        )
        raw_path = raw_path.lstrip("/")
        file_name = Path(raw_path).name

        candidates: list[str] = []
        if raw_path:
            candidates.append(raw_path)
        if file_name:
            candidates.append(f"xhtml/{file_name}")
            candidates.append(file_name)

        base_href: Optional[str] = None
        for candidate in candidates:
            if candidate in manifest_lookup:
                base_href = candidate
                break

        if base_href is None and file_name:
            for candidate in manifest_lookup:
                if Path(candidate).name == file_name:
                    base_href = candidate
                    break

        if base_href is None:
            self._warn("TOC references missing file: %s", reference_id)
            return None

        fragment = self._as_non_empty_str(entry.get("fragment"))
        return f"{base_href}#{fragment}" if fragment else base_href

    # ------------------------------------------------------------------
    # Resource detection helpers
    # ------------------------------------------------------------------
    def _detect_cover_image_href(self, identifier: str) -> Optional[str]:
        cover_doc = None
        text_dir = self._find_optional_dir("xhtml", "text")
        if text_dir is not None:
            for candidate in (text_dir / "cover.xhtml", text_dir / "cover.html"):
                if candidate.exists():
                    cover_doc = candidate
                    break

        if cover_doc is None:
            return None

        href = self._href_from_path(cover_doc)
        normalized = self._normalize_xhtml(cover_doc.read_bytes(), href, identifier)
        text = normalized.decode("utf-8", errors="ignore")
        match = re.search(r"<img\b[^>]*\bsrc=\"([^\"]+)\"", text, re.IGNORECASE)
        if not match:
            return None

        image_href = html.unescape(match.group(1)).strip().lstrip("./")
        while image_href.startswith("../"):
            image_href = image_href[3:]

        if not image_href:
            return None

        image_dir_name = self._image_dir_name()
        image_name = Path(image_href).name
        candidates = [
            image_href,
            f"{image_dir_name}/{image_name}",
            f"Images/{image_name}",
            f"images/{image_name}",
        ]
        for candidate in candidates:
            if self._source_path_from_href(candidate).exists():
                return candidate
        return f"{image_dir_name}/{image_name}"

    def _find_cover_document_href(
        self,
        manifest_lookup: Mapping[str, epub.EpubItem],
    ) -> Optional[str]:
        if "xhtml/cover.xhtml" in manifest_lookup:
            return "xhtml/cover.xhtml"
        for href in manifest_lookup:
            if Path(href).stem.lower() == "cover":
                return href
        return None

    def _nav_file_name(
        self,
        nav_source_href: Optional[str],
        manifest_items: Sequence[ManifestItem],
    ) -> str:
        if nav_source_href:
            nav_path = Path(nav_source_href)
            if nav_path.suffix.lower() == ".xhtml":
                return nav_source_href
            return nav_path.with_suffix(".xhtml").as_posix()
        html_parents = sorted(
            {
                Path(item.href).parent.as_posix()
                for item in manifest_items
                if item.media_type == "application/xhtml+xml"
            }
        )
        for parent in html_parents:
            if parent and parent != ".":
                return f"{parent}/nav.xhtml"
        return "nav.xhtml"

    def _source_path_from_href(self, href: str) -> Path:
        source_path = self.source_dir / href
        if source_path.exists():
            return source_path

        parts = href.split("/", 1)
        if len(parts) == 2:
            alternative = self.source_dir / parts[0] / parts[1]
            if alternative.exists():
                return alternative
        return source_path

    def _href_from_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.source_dir).as_posix()
        except ValueError:
            return path.name

    def _guess_media_type(self, path: Path) -> str:
        if path.suffix.lower() in {".xhtml", ".html"}:
            return "application/xhtml+xml"
        if path.suffix.lower() == ".otf":
            return "application/vnd.ms-opentype"

        media_type, _ = mimetypes.guess_type(path.name)
        if media_type:
            return media_type

        self._warn(
            "Unknown media type for %s; using application/octet-stream",
            path.name,
        )
        return "application/octet-stream"

    # ------------------------------------------------------------------
    # XHTML normalization
    # ------------------------------------------------------------------
    def _render_cover_xhtml(
        self,
        *,
        identifier: str,
        document_href: str,
        image_href: str,
        image_alt: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> str:
        """Render a normalized cover.xhtml document."""

        def _rewrite_href(href: str) -> str:
            api_pattern = re.compile(
                rf"https?://[^'\"]*/api/v\d+/epubs/urn:orm:book:{re.escape(identifier)}/files/",
                re.IGNORECASE,
            )
            root_pattern = re.compile(
                rf"/api/v\d+/epubs/urn:orm:book:{re.escape(identifier)}/files/",
                re.IGNORECASE,
            )
            cleaned = api_pattern.sub("", href)
            cleaned = root_pattern.sub("", cleaned)
            return cleaned

        normalized_href = self._normalize_resource_reference(
            _rewrite_href(image_href),
            current_href=document_href,
            identifier=identifier,
        )
        if not normalized_href:
            normalized_href = self._relative_href(
                document_href,
                f"{self._image_dir_name()}/cover.jpg",
            )
        alt_text = image_alt.strip() if image_alt else "Cover image"

        img_attrs = [
            'aria-label="cover"',
            'role="doc-cover"',
            'class="imagefp"',
            f'src="{html.escape(normalized_href)}"',
            f'alt="{html.escape(alt_text)}"',
        ]
        if width:
            img_attrs.append(f'width="{width}"')
        if height:
            img_attrs.append(f'height="{height}"')

        img_tag = "<img " + " ".join(img_attrs) + " />"
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">\n'
            "<head>\n"
            '<meta charset="utf-8" />\n'
            "<title>Cover</title>\n"
            "</head>\n"
            "<body>\n"
            '<div id="sbo-rt-content" class="cover-pg">\n'
            '<figure class="ipadfp">'
            '<span role="doc-pagebreak" id="pgi" aria-label="i"></span>'
            f"{img_tag}"
            "</figure>\n"
            "</div>\n"
            "</body>\n"
            "</html>\n"
        )

    def _normalize_xhtml(self, content: bytes, href: str, identifier: str) -> bytes:
        """Ensure XHTML resources are well-formed and use relative asset paths."""

        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return content

        text = text.replace("\r\n", "\n")
        if Path(href).stem.lower() == "cover":
            return self._normalize_cover(text, href, identifier)

        stripped = text.lstrip()
        xml_decl = ""
        if stripped.startswith("<?xml"):
            idx = stripped.find("?>")
            if idx != -1:
                xml_decl = stripped[: idx + 2]
                stripped = stripped[idx + 2 :].lstrip()
        else:
            xml_decl = '<?xml version="1.0" encoding="utf-8"?>'

        text_body = stripped
        if not re.search(r"<html\b", text_body, re.IGNORECASE):
            text_body = (
                "<html>\n"
                "<head>\n"
                '<meta charset="utf-8" />\n'
                "<title></title>\n"
                "</head>\n"
                "<body>\n"
                f"{text_body}\n"
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
            rf"<(?P<tag>{'|'.join(void_elements)})(?![A-Za-z0-9])(?P<attrs>[^<>]*?)(?<!/)>",
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

        title_text = Path(href).stem.replace("_", " ").strip() or "Document"
        title_pattern = re.compile(r"<title>\s*</title>", re.IGNORECASE)
        text_body = title_pattern.sub(
            f"<title>{html.escape(title_text.title())}</title>",
            text_body,
            count=1,
        )

        break_span_pattern = re.compile(
            r"(<span\s+class=\"break\"[^>]*>)(.*?)(</span>)",
            re.IGNORECASE | re.DOTALL,
        )

        def _inject_break(match: re.Match[str]) -> str:
            opening, content, closing = match.groups()
            if "<br" in content.lower():
                return match.group(0)
            text_only = re.sub(r"<[^>]+>", "", content or "")
            text_only = html.unescape(text_only).strip()
            letters = [ch for ch in text_only if ch.isalpha()]
            injection = (
                "<br />" if letters and all(ch.isupper() for ch in letters) else ": "
            )
            return f"{opening}{injection}{content}{closing}"

        text_body = break_span_pattern.sub(_inject_break, text_body)

        part_header_pattern = re.compile(
            r"(<h1\b[^>]*class=\"[^\"]*h1pt[^\"]*\"[^>]*>)(?P<body>.*?</h1>)",
            re.IGNORECASE | re.DOTALL,
        )

        def _center_part_header(match: re.Match[str]) -> str:
            opening = match.group(1)
            body = match.group("body")
            if "PART" not in body.upper():
                return match.group(0)

            style_pattern = re.compile(r"style=\"([^\"]*)\"", re.IGNORECASE)
            style_match = style_pattern.search(opening)
            if style_match:
                styles = style_match.group(1)
                if "text-align" in styles.lower():
                    return match.group(0)
                opening = (
                    opening[: style_match.start(1)]
                    + styles.rstrip("; ")
                    + "; text-align:center;"
                    + opening[style_match.end(1) :]
                )
            elif opening.endswith(">"):
                opening = opening[:-1] + ' style="text-align:center;">'
            return opening + body

        text_body = part_header_pattern.sub(_center_part_header, text_body)
        text_body = self._repair_common_markup_issues(text_body)

        text_body = self._rewrite_resource_attributes(text_body, href, identifier)
        text_body = self._rewrite_css_urls(text_body, href, identifier)

        return f"{xml_decl}\n{text_body.lstrip()}".encode("utf-8")

    def _normalize_css(self, content: bytes, href: str, identifier: str) -> bytes:
        """Normalize CSS ``url(...)`` references to match EPUB resource paths."""

        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return content

        normalized = self._rewrite_css_urls(
            text.replace("\r\n", "\n"), href, identifier
        )
        return normalized.encode("utf-8")

    def _normalize_cover(self, text: str, href: str, identifier: str) -> bytes:
        """Transform arbitrary cover markup into a standard cover.xhtml body."""

        img_match = re.search(r"<img\b[^>]*>", text, re.IGNORECASE)
        img_tag = img_match.group(0) if img_match else ""
        attr_pattern = re.compile(
            r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*')"
        )
        attrs = {
            name.lower(): value[1:-1] for name, value in attr_pattern.findall(img_tag)
        }

        def _to_int(value: Optional[str]) -> Optional[int]:
            if not value:
                return None
            digits = re.sub(r"[^0-9]", "", value.strip())
            if not digits:
                return None
            try:
                return int(digits)
            except ValueError:
                return None

        return self._render_cover_xhtml(
            identifier=identifier,
            document_href=href,
            image_href=attrs.get("src", ""),
            image_alt=attrs.get("alt"),
            width=_to_int(attrs.get("width")),
            height=_to_int(attrs.get("height")),
        ).encode("utf-8")

    def _repair_common_markup_issues(self, text: str) -> str:
        """Repair recurring markup issues found in downloaded chapter content."""

        text = re.sub(r"<col\s+group\s*/>", "<colgroup>", text, flags=re.IGNORECASE)
        text = re.sub(r"<col\s+group\s*>", "<colgroup>", text, flags=re.IGNORECASE)

        anchor_pattern = re.compile(
            r"<a\b(?P<attrs>[^>]*)\bname=(?P<quote>['\"])(?P<name>.*?)(?P=quote)(?P<rest>[^>]*)>",
            re.IGNORECASE,
        )

        def _promote_anchor(match: re.Match[str]) -> str:
            attrs = match.group("attrs")
            rest = match.group("rest")
            if re.search(r"\bid\s*=", f"{attrs} {rest}", re.IGNORECASE):
                return match.group(0)

            quote = match.group("quote")
            name = match.group("name")
            return (
                f"<a{attrs}name={quote}{name}{quote}" f" id={quote}{name}{quote}{rest}>"
            )

        text = anchor_pattern.sub(_promote_anchor, text)

        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return text

        ET.register_namespace("", "http://www.w3.org/1999/xhtml")
        ET.register_namespace("epub", "http://www.idpf.org/2007/ops")

        changed = False
        for elem in root.iter():
            local_name = self._xml_local_name(elem.tag)
            if local_name == "acronym":
                elem.tag = self._replace_xml_local_name(elem.tag, "abbr")
                changed = True

            css_updates: list[str] = []

            align = elem.attrib.pop("align", None)
            if align:
                css_updates.append(f"text-align:{align.strip()}")
                changed = True

            valign = elem.attrib.pop("valign", None)
            if valign:
                css_updates.append(f"vertical-align:{valign.strip()}")
                changed = True

            border = elem.attrib.pop("border", None)
            if border is not None:
                border_value = border.strip()
                if border_value == "0":
                    css_updates.append("border:none")
                elif border_value.isdigit():
                    css_updates.append(f"border:{border_value}px solid")
                changed = True

            if "summary" in elem.attrib:
                del elem.attrib["summary"]
                changed = True

            if css_updates:
                elem.attrib["style"] = self._merge_inline_styles(
                    elem.attrib.get("style"),
                    css_updates,
                )

        if not changed:
            return text
        return ET.tostring(root, encoding="unicode")

    def _normalize_html_boolean_attributes(self, text: str) -> str:
        """Rewrite HTML boolean attributes into XML-safe attribute syntax."""

        boolean_attrs = ("async", "defer", "checked", "disabled", "selected")

        def _normalize_tag(match: re.Match[str]) -> str:
            tag_text = match.group(0)
            for attr_name in boolean_attrs:
                tag_text = re.sub(
                    rf"\b{attr_name}\b(?!\s*=)",
                    f'{attr_name}="{attr_name}"',
                    tag_text,
                    flags=re.IGNORECASE,
                )
            return tag_text

        return re.sub(r"<script\b[^>]*>", _normalize_tag, text, flags=re.IGNORECASE)

    def _strip_unresolvable_resource_tags(
        self,
        text: str,
        member_name: str,
        *,
        member_lookup: Mapping[str, str],
        basename_lookup: Mapping[str, Sequence[str]],
    ) -> str:
        """Remove script tags and link tags with invalid/unresolvable hrefs."""

        def _maybe_remove_link(match: re.Match[str]) -> str:
            tag_text = match.group(0)
            href_match = re.search(
                r'\bhref\s*=\s*["\']([^"\']+)["\']',
                tag_text,
                re.IGNORECASE,
            )
            if href_match and self._is_unresolved_archive_reference(
                href_match.group(1),
                member_name,
                member_lookup=member_lookup,
                basename_lookup=basename_lookup,
            ):
                return ""
            return tag_text

        text = re.sub(
            r"<script\b[^>]*>.*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL
        )
        return re.sub(
            r"<link\b[^>]*?/?>",
            _maybe_remove_link,
            text,
            flags=re.IGNORECASE,
        )

    def _prune_unresolved_archive_references(
        self,
        text: str,
        member_name: str,
        *,
        member_lookup: Mapping[str, str],
        basename_lookup: Mapping[str, Sequence[str]],
    ) -> str:
        """Remove stale overlay blocks or unresolved resource attributes post-parse."""

        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return text

        changed = False

        def _walk(parent: ET.Element) -> None:
            nonlocal changed
            for child in list(parent):
                _walk(child)

                local_name = self._xml_local_name(child.tag)
                if local_name == "div" and child.attrib.get("id") == "sec-overlay":
                    parent.remove(child)
                    changed = True
                    continue

                for attr_name in ("src", "href", "poster"):
                    attr_value = child.attrib.get(attr_name)
                    if not attr_value:
                        continue
                    if attr_name == "href" and self._href_requires_removal(attr_value):
                        del child.attrib[attr_name]
                        changed = True
                        continue
                    if self._is_unresolved_archive_reference(
                        attr_value,
                        member_name,
                        member_lookup=member_lookup,
                        basename_lookup=basename_lookup,
                    ):
                        del child.attrib[attr_name]
                        changed = True

        _walk(root)
        if not changed:
            return text
        return ET.tostring(root, encoding="unicode")

    def _is_unresolved_archive_reference(
        self,
        value: str,
        member_name: str,
        *,
        member_lookup: Mapping[str, str],
        basename_lookup: Mapping[str, Sequence[str]],
    ) -> bool:
        stripped_value = html.unescape(value).strip()
        if not stripped_value:
            return False

        parsed = urlsplit(stripped_value)
        if parsed.scheme or parsed.netloc or not parsed.path:
            return False

        if self._href_requires_removal(stripped_value):
            return True

        target_member = self._resolve_archive_member(
            parsed.path,
            member_name,
            member_lookup=member_lookup,
            basename_lookup=basename_lookup,
        )
        return target_member is None

    def _href_requires_removal(self, href: str) -> bool:
        """Return ``True`` when an href should be stripped from cleaned output."""

        stripped_href = html.unescape(href).strip()
        if not stripped_href or stripped_href.startswith("#"):
            return False

        parsed = urlsplit(stripped_href)
        path = parsed.path
        if not path:
            return False

        return PurePosixPath(path).suffix == ""

    def _rewrite_resource_attributes(
        self,
        text: str,
        current_href: str,
        identifier: str,
    ) -> str:
        attr_pattern = re.compile(
            r'(?P<prefix>\b(?:href|src|poster)\s*=\s*)(?P<quote>["\'])(?P<value>.*?)(?P=quote)',
            re.IGNORECASE,
        )

        def _rewrite(match: re.Match[str]) -> str:
            original_value = match.group("value")
            normalized_value = self._normalize_resource_reference(
                original_value,
                current_href=current_href,
                identifier=identifier,
            )
            quote = match.group("quote")
            escaped = html.escape(normalized_value, quote=True)
            return f"{match.group('prefix')}{quote}{escaped}{quote}"

        return attr_pattern.sub(_rewrite, text)

    def _rewrite_css_urls(
        self,
        text: str,
        current_href: str,
        identifier: str,
    ) -> str:
        url_pattern = re.compile(
            r"url\(\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^)\s]+))\s*\)",
            re.IGNORECASE,
        )

        def _rewrite(match: re.Match[str]) -> str:
            value = (
                match.group("double")
                if match.group("double") is not None
                else (
                    match.group("single")
                    if match.group("single") is not None
                    else match.group("bare") or ""
                )
            )
            normalized_value = self._normalize_resource_reference(
                value,
                current_href=current_href,
                identifier=identifier,
            )
            if match.group("double") is not None:
                return f'url("{normalized_value}")'
            if match.group("single") is not None:
                return f"url('{normalized_value}')"
            return f"url({normalized_value})"

        return url_pattern.sub(_rewrite, text)

    def _normalize_resource_reference(
        self,
        value: str,
        *,
        current_href: str,
        identifier: str,
    ) -> str:
        normalized_value = self._strip_api_file_prefix(
            html.unescape(value).strip(), identifier
        )
        if not normalized_value or normalized_value.startswith("#"):
            return normalized_value

        parsed = urlsplit(normalized_value)
        if parsed.scheme.lower() in {"data", "mailto", "tel", "javascript"}:
            return normalized_value
        if parsed.scheme or parsed.netloc:
            return normalized_value

        target_href = self._resolve_resource_href(parsed.path, current_href)
        if target_href is None:
            return normalized_value

        relative_href = self._relative_href(current_href, target_href)
        if parsed.query:
            relative_href = f"{relative_href}?{parsed.query}"
        if parsed.fragment:
            relative_href = f"{relative_href}#{parsed.fragment}"
        return relative_href

    def _strip_api_file_prefix(self, value: str, identifier: str) -> str:
        api_pattern = re.compile(
            rf"https?://[^'\"]*/api/v\d+/epubs/urn:orm:book:{re.escape(identifier)}/files/",
            re.IGNORECASE,
        )
        root_pattern = re.compile(
            rf"/api/v\d+/epubs/urn:orm:book:{re.escape(identifier)}/files/",
            re.IGNORECASE,
        )
        cleaned = api_pattern.sub("", value)
        cleaned = root_pattern.sub("", cleaned)
        return cleaned.replace("\\", "/")

    def _resolve_resource_href(
        self,
        raw_path: str,
        current_href: str,
    ) -> Optional[str]:
        path = raw_path.strip()
        if not path:
            return None

        current_dir = posixpath.dirname(current_href) or "."
        candidates = [
            (
                posixpath.normpath(posixpath.join(current_dir, path))
                if not path.startswith("/")
                else posixpath.normpath(path.lstrip("/"))
            ),
            posixpath.normpath(path.lstrip("/")),
        ]

        for candidate in candidates:
            if candidate in {"", "."}:
                continue
            actual_href = self._resource_href_lookup.get(candidate.lower())
            if actual_href is not None:
                return actual_href

        file_name = PurePosixPath(path).name.lower()
        if file_name:
            matches = self._resource_hrefs_by_name.get(file_name, [])
            if len(matches) == 1:
                return matches[0]

        return self._fallback_resource_href(path)

    def _fallback_resource_href(self, raw_path: str) -> Optional[str]:
        path = PurePosixPath(raw_path.lstrip("/"))
        file_name = path.name
        if not file_name:
            return None

        target_dir = self._resource_dir_name_for_suffix(path.suffix.lower())
        if target_dir is None:
            return None

        candidate = f"{target_dir}/{file_name}"
        candidate_path = self._source_path_from_href(candidate)
        if candidate_path.exists():
            return self._href_from_path(candidate_path)
        return None

    def _relative_href(self, current_href: str, target_href: str) -> str:
        current_dir = posixpath.dirname(current_href) or "."
        return posixpath.relpath(target_href, start=current_dir)

    # ------------------------------------------------------------------
    # Output patching helpers
    # ------------------------------------------------------------------
    def _patch_opf_version(self, output_path: Path, *, version: str) -> None:
        """Rewrite content.opf version for tools that prefer OPF 2.0 metadata."""

        temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
        try:
            with ZipFile(output_path, "r") as source_zip:
                with ZipFile(temp_path, "w") as target_zip:
                    for info in source_zip.infolist():
                        data = source_zip.read(info.filename)
                        if info.filename == "OEBPS/content.opf":
                            data = re.sub(
                                rb'version="[^"]+"',
                                f'version="{version}"'.encode("utf-8"),
                                data,
                                count=1,
                            )
                        if info.filename == "mimetype":
                            info.compress_type = ZIP_STORED
                        target_zip.writestr(info, data)
            os.replace(temp_path, output_path)
        except Exception as exc:  # pragma: no cover - defensive
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            self._warn("Unable to patch content.opf version: %s", exc)

    def _cleanup_output_archive(self, output_path: Path, *, calibre: bool) -> None:
        """Apply post-build cleanups to the generated EPUB archive."""

        temp_path = output_path.with_suffix(output_path.suffix + ".clean")
        try:
            with ZipFile(output_path, "r") as source_zip:
                infos = source_zip.infolist()
                archive_contents = {
                    info.filename: source_zip.read(info.filename) for info in infos
                }

            removed_members = self._members_to_remove(archive_contents, calibre=calibre)
            anchor_map = self._collect_archive_anchor_map(
                {
                    name: data
                    for name, data in archive_contents.items()
                    if name not in removed_members
                }
            )
            member_lookup = {
                name.lower(): name
                for name in archive_contents
                if name not in removed_members
            }
            basename_lookup: dict[str, list[str]] = {}
            for name in archive_contents:
                if name in removed_members:
                    continue
                basename_lookup.setdefault(PurePosixPath(name).name.lower(), []).append(
                    name
                )

            with ZipFile(temp_path, "w") as target_zip:
                for info in infos:
                    name = info.filename
                    if name in removed_members:
                        continue

                    data = archive_contents[name]
                    suffix = PurePosixPath(name).suffix.lower()
                    if suffix in {".xhtml", ".html"}:
                        data = self._cleanup_output_document(
                            data,
                            name,
                            calibre=calibre,
                            anchor_map=anchor_map,
                            member_lookup=member_lookup,
                            basename_lookup=basename_lookup,
                        )
                    elif suffix == ".ncx":
                        data = self._cleanup_output_ncx(
                            data,
                            name,
                            anchor_map=anchor_map,
                            member_lookup=member_lookup,
                            basename_lookup=basename_lookup,
                        )
                    elif suffix == ".opf":
                        data = self._cleanup_output_opf(
                            data,
                            calibre=calibre,
                            removed_members=removed_members,
                        )

                    if info.filename == "mimetype":
                        info.compress_type = ZIP_STORED
                    target_zip.writestr(info, data)

            os.replace(temp_path, output_path)
        except Exception as exc:  # pragma: no cover - defensive
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            self._warn("Unable to clean generated EPUB archive: %s", exc)

    def _members_to_remove(
        self,
        archive_contents: Mapping[str, bytes],
        *,
        calibre: bool,
    ) -> set[str]:
        if not calibre:
            return set()

        return {
            name
            for name in archive_contents
            if PurePosixPath(name).name.lower() == "nav.xhtml"
        }

    def _collect_archive_anchor_map(
        self,
        archive_contents: Mapping[str, bytes],
    ) -> dict[str, set[str]]:
        anchor_map: dict[str, set[str]] = {}
        id_pattern = re.compile(
            r"\b(?:id|xml:id)\s*=\s*['\"]([^'\"]+)['\"]",
            re.IGNORECASE,
        )
        name_pattern = re.compile(
            r"<a\b[^>]*\bname\s*=\s*['\"]([^'\"]+)['\"]",
            re.IGNORECASE,
        )

        for name, data in archive_contents.items():
            if PurePosixPath(name).suffix.lower() not in {".xhtml", ".html"}:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            anchors = set(id_pattern.findall(text))
            anchors.update(name_pattern.findall(text))
            anchor_map[name] = anchors

        return anchor_map

    def _cleanup_output_document(
        self,
        data: bytes,
        member_name: str,
        *,
        calibre: bool,
        anchor_map: Mapping[str, set[str]],
        member_lookup: Mapping[str, str],
        basename_lookup: Mapping[str, Sequence[str]],
    ) -> bytes:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return data

        if calibre:
            text = re.sub(
                r"<meta\s+charset=(['\"]).*?\1\s*/>",
                '<meta http-equiv="Content-Type" content="text/html; charset=utf-8" />',
                text,
                flags=re.IGNORECASE,
            )

        text = self._normalize_html_boolean_attributes(text)
        text = self._strip_unresolvable_resource_tags(
            text,
            member_name,
            member_lookup=member_lookup,
            basename_lookup=basename_lookup,
        )
        text = self._repair_common_markup_issues(text)
        text = self._prune_unresolved_archive_references(
            text,
            member_name,
            member_lookup=member_lookup,
            basename_lookup=basename_lookup,
        )
        text = self._cleanup_archive_links(
            text,
            member_name,
            anchor_map=anchor_map,
            member_lookup=member_lookup,
            basename_lookup=basename_lookup,
        )
        return text.encode("utf-8")

    def _cleanup_output_ncx(
        self,
        data: bytes,
        member_name: str,
        *,
        anchor_map: Mapping[str, set[str]],
        member_lookup: Mapping[str, str],
        basename_lookup: Mapping[str, Sequence[str]],
    ) -> bytes:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return data

        text = self._cleanup_archive_links(
            text,
            member_name,
            anchor_map=anchor_map,
            member_lookup=member_lookup,
            basename_lookup=basename_lookup,
            attribute_names=("src",),
        )
        return text.encode("utf-8")

    def _cleanup_output_opf(
        self,
        data: bytes,
        *,
        calibre: bool,
        removed_members: set[str],
    ) -> bytes:
        if not calibre:
            return data

        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return data

        opf_ns = "http://www.idpf.org/2007/opf"
        dc_ns = "http://purl.org/dc/elements/1.1/"
        ET.register_namespace("", opf_ns)
        ET.register_namespace("dc", dc_ns)
        ET.register_namespace("opf", opf_ns)

        removed_hrefs = {
            member.split("/", 1)[1] if "/" in member else member
            for member in removed_members
        }

        root.attrib.pop("prefix", None)
        root.attrib["version"] = "2.0"

        metadata = root.find(f"{{{opf_ns}}}metadata")
        if metadata is not None:
            for child in list(metadata):
                if self._xml_local_name(child.tag) != "meta":
                    continue
                if "property" in child.attrib or "refines" in child.attrib:
                    metadata.remove(child)

        removed_ids: set[str] = set()
        manifest = root.find(f"{{{opf_ns}}}manifest")
        if manifest is not None:
            for item in list(manifest):
                if self._xml_local_name(item.tag) != "item":
                    continue
                item.attrib.pop("properties", None)
                href = item.attrib.get("href", "")
                if (
                    href in removed_hrefs
                    or PurePosixPath(href).name.lower() == "nav.xhtml"
                ):
                    item_id = item.attrib.get("id")
                    if item_id:
                        removed_ids.add(item_id)
                    manifest.remove(item)

        spine = root.find(f"{{{opf_ns}}}spine")
        if spine is not None:
            for itemref in list(spine):
                if self._xml_local_name(itemref.tag) != "itemref":
                    continue
                idref = itemref.attrib.get("idref")
                if idref == "nav" or idref in removed_ids:
                    spine.remove(itemref)

        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def _cleanup_archive_links(
        self,
        text: str,
        member_name: str,
        *,
        anchor_map: Mapping[str, set[str]],
        member_lookup: Mapping[str, str],
        basename_lookup: Mapping[str, Sequence[str]],
        attribute_names: Sequence[str] = ("href", "src", "poster"),
    ) -> str:
        attr_pattern = re.compile(
            rf'(?P<prefix>\b(?:{"|".join(attribute_names)})\s*=\s*)(?P<quote>["\'])(?P<value>.*?)(?P=quote)',
            re.IGNORECASE,
        )

        def _rewrite(match: re.Match[str]) -> str:
            value = html.unescape(match.group("value"))
            cleaned_value = self._cleanup_archive_reference_value(
                value,
                member_name,
                anchor_map=anchor_map,
                member_lookup=member_lookup,
                basename_lookup=basename_lookup,
            )
            quote = match.group("quote")
            escaped = html.escape(cleaned_value, quote=True)
            return f"{match.group('prefix')}{quote}{escaped}{quote}"

        return attr_pattern.sub(_rewrite, text)

    def _cleanup_archive_reference_value(
        self,
        value: str,
        member_name: str,
        *,
        anchor_map: Mapping[str, set[str]],
        member_lookup: Mapping[str, str],
        basename_lookup: Mapping[str, Sequence[str]],
    ) -> str:
        stripped_value = value.strip()
        if not stripped_value:
            return stripped_value

        parsed = urlsplit(stripped_value)
        if parsed.scheme.lower() in {"data", "mailto", "tel", "javascript"}:
            return stripped_value
        if parsed.scheme or parsed.netloc:
            return stripped_value

        target_member = self._resolve_archive_member(
            parsed.path,
            member_name,
            member_lookup=member_lookup,
            basename_lookup=basename_lookup,
        )
        if target_member is None:
            return stripped_value

        fragment = parsed.fragment
        if fragment:
            fragment = self._resolve_archive_fragment(
                fragment, anchor_map.get(target_member, set())
            )

        if parsed.path:
            relative_path = posixpath.relpath(
                target_member,
                start=posixpath.dirname(member_name) or ".",
            )
        else:
            relative_path = ""

        return urlunsplit(("", "", relative_path, parsed.query, fragment or ""))

    def _resolve_archive_member(
        self,
        raw_path: str,
        current_member: str,
        *,
        member_lookup: Mapping[str, str],
        basename_lookup: Mapping[str, Sequence[str]],
    ) -> Optional[str]:
        if not raw_path:
            return current_member

        candidate_paths = [
            posixpath.normpath(
                posixpath.join(posixpath.dirname(current_member) or ".", raw_path)
            ),
            posixpath.normpath(raw_path.lstrip("/")),
        ]
        for candidate in candidate_paths:
            match = member_lookup.get(candidate.lower())
            if match is not None:
                return match

        basename = PurePosixPath(raw_path).name.lower()
        matches = basename_lookup.get(basename, [])
        if len(matches) == 1:
            return matches[0]
        return None

    def _resolve_archive_fragment(
        self,
        fragment: str,
        anchors: set[str],
    ) -> Optional[str]:
        if fragment in anchors:
            return fragment

        lower_matches = [
            anchor for anchor in anchors if anchor.lower() == fragment.lower()
        ]
        if len(lower_matches) == 1:
            return lower_matches[0]
        return None

    def _run_epubcheck(self, output_path: Path) -> None:
        """Validate the generated EPUB with epubcheck."""

        checker = self._find_epubchecker()
        if checker is None:
            raise RuntimeError(
                "epubcheck validation was requested, but no epubcheck executable "
                "was found in PATH. Install epubcheck and retry, or omit --check."
            )

        checker_name, checker_path = checker
        try:
            result = subprocess.run(
                [checker_path, str(output_path)],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:  # pragma: no cover - defensive
            self._warn("Unable to run %s: %s", checker_name, exc)
            return

        checker_output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        fatal_count, error_count, warning_count = self._parse_epubcheck_counts(
            checker_output
        )
        checker_summary = self._summarize_epubcheck_output(
            checker_output,
            returncode=result.returncode,
            fatal_count=fatal_count,
            error_count=error_count,
            warning_count=warning_count,
        )
        show_full_output = self._verbose or self._debug
        if fatal_count or error_count:
            checker_message = checker_output if show_full_output else checker_summary
            if not show_full_output:
                checker_message = f"{checker_message}\nUse --verbose to see the full epubcheck output."
            raise RuntimeError(
                f"{checker_name} validation failed for {output_path.name}:\n{checker_message}"
            )

        if result.returncode != 0 and fatal_count is None and error_count is None:
            checker_message = checker_output if show_full_output else checker_summary
            if not show_full_output:
                checker_message = f"{checker_message}\nUse --verbose to see the full epubcheck output."
            raise RuntimeError(
                f"{checker_name} returned a non-zero exit status for {output_path.name}:\n{checker_message}"
            )

        if checker_output:
            if show_full_output:
                self._warn(
                    "%s output for %s:\n%s",
                    checker_name,
                    output_path.name,
                    checker_output,
                )
            else:
                self._warn(
                    "%s summary for %s: %s",
                    checker_name,
                    output_path.name,
                    checker_summary,
                )

    def _find_epubchecker(self) -> tuple[str, str] | None:
        for executable_name in ("epubchecker", "epubcheck"):
            executable_path = shutil.which(executable_name)
            if executable_path:
                return executable_name, executable_path
        return None

    def _parse_epubcheck_counts(
        self,
        checker_output: str,
    ) -> tuple[Optional[int], Optional[int], Optional[int]]:
        match = re.search(
            r"Messages:\s*(\d+)\s+fatals?\s*/\s*(\d+)\s+errors?\s*/\s*(\d+)\s+warnings?",
            checker_output,
            re.IGNORECASE,
        )
        if match is None:
            return None, None, None
        fatal_count = int(match.group(1))
        error_count = int(match.group(2))
        warning_count = int(match.group(3))
        return fatal_count, error_count, warning_count

    def _summarize_epubcheck_output(
        self,
        checker_output: str,
        *,
        returncode: int,
        fatal_count: Optional[int],
        error_count: Optional[int],
        warning_count: Optional[int],
    ) -> str:
        summary_match = re.search(
            r"Messages:\s*\d+\s+fatals?\s*/\s*\d+\s+errors?\s*/\s*\d+\s+warnings?",
            checker_output,
            re.IGNORECASE,
        )
        if summary_match is not None:
            return " ".join(summary_match.group(0).split())

        if (
            fatal_count is not None
            and error_count is not None
            and warning_count is not None
        ):
            return (
                "Messages: "
                f"{fatal_count} fatals / {error_count} errors / {warning_count} warnings"
            )

        first_line = next(
            (line.strip() for line in checker_output.splitlines() if line.strip()),
            "",
        )
        if first_line:
            return first_line

        return f"epubcheck exited with status {returncode}"

    def _xml_local_name(self, tag: str) -> str:
        if "}" in tag:
            return tag.split("}", 1)[1]
        return tag

    def _replace_xml_local_name(self, tag: str, local_name: str) -> str:
        if "}" in tag:
            namespace = tag.split("}", 1)[0][1:]
            return f"{{{namespace}}}{local_name}"
        return local_name

    def _merge_inline_styles(
        self,
        existing_style: Optional[str],
        additions: Sequence[str],
    ) -> str:
        style_map: dict[str, str] = {}

        if existing_style:
            for declaration in existing_style.split(";"):
                if ":" not in declaration:
                    continue
                key, value = declaration.split(":", 1)
                key = key.strip().lower()
                value = value.strip()
                if key and value:
                    style_map[key] = value

        for declaration in additions:
            if ":" not in declaration:
                continue
            key, value = declaration.split(":", 1)
            key = key.strip().lower()
            value = value.strip()
            if key and value:
                style_map[key] = value

        return "; ".join(f"{key}:{value}" for key, value in style_map.items())

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _manifest_id_from_href(self, href: str) -> str:
        base = Path(href).stem
        if "images" in href.lower():
            base = "img_" + base
        sanitized = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in base)
        sanitized = sanitized.lstrip("_") or "item"
        if not sanitized[0].isalpha() and sanitized[0] != "_":
            sanitized = f"item_{sanitized}"

        candidate = sanitized
        counter = 1
        while candidate in self._manifest_ids:
            counter += 1
            candidate = f"{sanitized}_{counter}"
        return candidate

    def _toc_uid(self, href: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9_]+", "_", href).strip("_")
        return f"toc_{sanitized or 'entry'}"

    def _title_from_href(self, href: str) -> str:
        stem = Path(href.split("#", 1)[0]).stem.replace("_", " ").strip()
        return stem.title() if stem else "Document"

    def _find_optional_dir(self, *names: str) -> Optional[Path]:
        for name in names:
            candidate = self.source_dir / name
            if candidate.exists() and candidate.is_dir():
                return candidate
        return None

    def _image_dir_name(self) -> str:
        image_dir = self._find_optional_dir("Images", "images")
        if image_dir is not None:
            return image_dir.name
        return "Images"

    def _style_dir_name(self) -> str:
        styles_dir = self._find_optional_dir("Styles", "styles")
        if styles_dir is not None:
            return styles_dir.name
        return "Styles"

    def _font_dir_name(self) -> str:
        fonts_dir = self._find_optional_dir("fonts", "Fonts")
        if fonts_dir is not None:
            return fonts_dir.name
        return "fonts"

    def _text_dir_name(self) -> str:
        text_dir = self._find_optional_dir("xhtml", "text")
        if text_dir is not None:
            return text_dir.name
        return "xhtml"

    def _resource_dir_name_for_suffix(self, suffix: str) -> Optional[str]:
        if suffix in {
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
            return self._image_dir_name()
        if suffix == ".css":
            return self._style_dir_name()
        if suffix in {".ttf", ".otf", ".woff", ".woff2", ".eot"}:
            return self._font_dir_name()
        if suffix in {".xhtml", ".html"}:
            return self._text_dir_name()
        return None

    def _as_non_empty_str(self, value: object) -> Optional[str]:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return None

    def _unique_strings(self, values: Sequence[str]) -> list[str]:
        results: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = value.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            results.append(normalized)
        return results

    def _warn(self, message: str, *args: object) -> None:
        if args:
            message = message % args
        self.warnings.append(message)
