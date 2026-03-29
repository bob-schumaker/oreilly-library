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

import datetime
import html
import json
import mimetypes
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
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
mimetypes.add_type("font/otf", ".otf")
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
    ) -> None:
        self.source_dir = Path(source_dir).resolve()
        self.output_dir = (
            Path(output_dir).resolve() if output_dir is not None else self.source_dir
        )
        self.warnings: list[str] = []
        self._manifest_ids: set[str] = set()

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

        modified = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        book.add_metadata(
            None,
            "meta",
            modified.isoformat().replace("+00:00", "Z"),
            {"property": "dcterms:modified"},
        )

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

    # ------------------------------------------------------------------
    # TOC helpers
    # ------------------------------------------------------------------
    def _build_toc(
        self,
        manifest_lookup: Mapping[str, epub.EpubItem],
        spine_items: Sequence[Path],
    ) -> tuple[Any, ...]:
        raw_toc = self._load_toc_entries()
        if raw_toc:
            built: list[Any] = []
            for entry in raw_toc:
                node = self._build_toc_node(entry, manifest_lookup)
                if node is not None:
                    built.append(node)
            if built:
                return tuple(built)

        fallback: list[epub.Link] = []
        for path in spine_items:
            href = self._href_from_path(path)
            if href in manifest_lookup:
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
    ) -> Any | None:
        href = self._resolve_toc_href(entry, manifest_lookup)
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

        normalized_href = _rewrite_href(image_href).strip()
        if normalized_href.lower().startswith("images/"):
            normalized_href = normalized_href[7:]
        elif normalized_href.lower().startswith("image/"):
            normalized_href = normalized_href[6:]
        elif normalized_href:
            normalized_href = Path(normalized_href).name
        if not normalized_href:
            normalized_href = "cover.jpg"

        normalized_href = f"{self._image_dir_name()}/{normalized_href}"
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
            return self._normalize_cover(text, identifier)

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

        depth = max(len(Path(href).parts) - 1, 0)
        prefix = "../" * depth
        api_pattern = re.compile(
            rf"https?://[^'\"]*/api/v\d+/epubs/urn:orm:book:{re.escape(identifier)}/files/",
            re.IGNORECASE,
        )
        text_body = api_pattern.sub(prefix, text_body)

        api_root_pattern = re.compile(
            rf"/api/v\d+/epubs/urn:orm:book:{re.escape(identifier)}/files/",
            re.IGNORECASE,
        )
        text_body = api_root_pattern.sub(prefix, text_body)

        return f"{xml_decl}\n{text_body.lstrip()}".encode("utf-8")

    def _normalize_cover(self, text: str, identifier: str) -> bytes:
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
            image_href=attrs.get("src", ""),
            image_alt=attrs.get("alt"),
            width=_to_int(attrs.get("width")),
            height=_to_int(attrs.get("height")),
        ).encode("utf-8")

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

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _manifest_id_from_href(self, href: str) -> str:
        base = Path(href).stem
        if "Images" in href:
            base = "img_" + base
        sanitized = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in base)
        sanitized = sanitized.lstrip("_") or "item"

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
