"""Utilities for assembling simple EPUB 3 archives from local folders.

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
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

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


@dataclass
class ManifestItem:
    """Representation of an item inside the OPF manifest."""

    item_id: str
    href: str
    media_type: str
    properties: Optional[str] = None


class EpubBuilder:
    """Create an EPUB 3 archive from a directory tree similar to ``data/``."""

    def __init__(
        self,
        source_dir: Path | str,
        output_dir: Optional[Path | str] = None,
    ) -> None:
        self.source_dir = Path(source_dir).resolve()
        self.output_dir = (
            Path(output_dir).resolve() if output_dir is not None else self.source_dir
        )
        self.warnings: List[str] = []
        self._manifest_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def build_epub(self, calibre: bool = False) -> Path:
        """Build an EPUB file and return the resulting path."""

        self.warnings.clear()
        metadata = self._load_metadata()
        opf_metadata = self._load_opf_metadata()
        identifier = metadata.get("identifier") or metadata.get("isbn")
        if not identifier:
            identifier = self.source_dir.name
            self._warn(
                "Metadata missing identifier; using directory name %s",
                identifier,
            )

        # Yes, there must be a better way
        categories = metadata.get("categories", [[]])[0]
        if metadata.get("roughcut"):
            categories.append("early release")

        title = metadata.get("title") or identifier
        language = metadata.get("language", "en")
        unique_id = metadata.get("opf_unique_identifier_type", "bookid")
        publication_date = metadata.get("publication_date")
        authors = metadata.get("authors") or opf_metadata.get("authors", [])
        publishers = opf_metadata.get("publishers", [])
        description_html = None
        descriptions = metadata.get("descriptions")
        if isinstance(descriptions, dict):
            description_html = descriptions.get("text/html")

        spine_items = self._resolve_spine()
        manifest_items, nav_id = self._build_manifest(spine_items)

        output_path = self.output_dir / f"{identifier}.epub"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with ZipFile(output_path, "w", compression=ZIP_DEFLATED) as archive:
            self._write_mimetype(archive)
            self._write_container_xml(archive)
            self._write_resources(archive, manifest_items, identifier)
            opf_bytes = self._render_content_opf(
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
                nav_id=nav_id,
                version_id="2.0" if calibre else None,
            )
            archive.writestr("OEBPS/content.opf", opf_bytes)

        for warning in self.warnings:
            print(f"Warning: {warning}")

        return output_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _load_metadata(self) -> Dict:
        json_files = list(self.source_dir.glob("*.json"))
        for meta_path in json_files:
            try:
                with meta_path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except Exception as exc:  # pragma: no cover - defensive
                self._warn(
                    "Failed to read metadata file %s: %s",
                    meta_path.name,
                    exc,
                )
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
                    return json.load(handle)
            except Exception as exc:  # pragma: no cover - defensive
                self._warn(
                    "Failed to parse fallback metadata file %s: %s",
                    fallback.name,
                    exc,
                )

        self._warn("No metadata JSON files found in %s", self.source_dir)
        return {}

    def _resolve_spine(self) -> List[Path]:
        spine_path = self.source_dir / "spine.json"
        text_dir = self.source_dir / "xhtml"
        if spine_path.exists():
            try:
                with spine_path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                results = data.get("results") or []
                resolved: List[Path] = []
                for entry in results:
                    ref_id = entry.get("reference_id")
                    if not ref_id:
                        continue
                    filename = ref_id.split("/")[-1]
                    candidate = text_dir / filename
                    if candidate.exists():
                        resolved.append(candidate)
                    else:
                        self._warn(
                            "Spine references missing file: %s",
                            filename,
                        )
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

    def _load_opf_metadata(self) -> Dict[str, List[str]]:
        """Extract author and publisher details from any OPF file present."""

        authors: List[str] = []
        publishers: List[str] = []
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

    def _build_manifest(
        self,
        spine_items: Sequence[Path],
    ) -> Tuple[List[ManifestItem], Optional[str]]:
        manifest: List[ManifestItem] = []
        self._manifest_ids.clear()
        nav_id: Optional[str] = None

        def register(path: Path, href: str, properties: Optional[str] = None) -> str:
            item_id = self._manifest_id_from_href(href)
            media_type, _ = mimetypes.guess_type(path.name)
            if not media_type:
                media_type = "application/octet-stream"
                self._warn(
                    "Unknown media type for %s; using application/octet-stream",
                    path.name,
                )
            manifest.append(
                ManifestItem(
                    item_id=item_id,
                    href=href,
                    media_type=media_type,
                    properties=properties,
                )
            )
            self._manifest_ids.add(item_id)
            return item_id

        text_dir = self.source_dir / "xhtml"
        if text_dir.exists():
            for html_ext in ("html", "xhtml"):
                for path in sorted(text_dir.glob(f"*.{html_ext}")):
                    href = f"xhtml/{path.name}"
                    props = "nav" if path.name.lower() == "nav.{html_ext}" else None
                    item_id = register(path, href, props)
                    if props == "nav":
                        nav_id = item_id
        else:
            self._warn("Missing text directory at %s", text_dir)

        styles_dir = self.source_dir / "Styles"
        if styles_dir.exists():
            for path in sorted(styles_dir.glob("*")):
                if path.is_file():
                    href = f"Styles/{path.name}"
                    register(path, href)
        else:
            self._warn("Missing styles directory at %s", styles_dir)

        fonts_dir = self.source_dir / "fonts"
        if fonts_dir.exists():
            for path in sorted(fonts_dir.glob("*")):
                if path.is_file():
                    href = f"fonts/{path.name}"
                    register(path, href)

        images_dir = self.source_dir / "Images"
        if images_dir.exists():
            for path in sorted(images_dir.glob("*")):
                if path.is_file():
                    href = f"Images/{path.name}"
                    register(path, href)
        else:
            self._warn("Missing images directory at %s", images_dir)

        manifest_lookup = {item.href: item.item_id for item in manifest}
        for path in spine_items:
            try:
                href = path.relative_to(self.source_dir).as_posix()
            except ValueError:
                href = path.name
            if href not in manifest_lookup:
                item_id = register(path, href)
                manifest_lookup[href] = item_id

        return manifest, nav_id

    def _write_resources(
        self,
        archive: ZipFile,
        manifest_items: Iterable[ManifestItem],
        identifier: str,
    ) -> None:
        for item in manifest_items:
            source_path = self.source_dir / item.href
            if not source_path.exists():
                parts = item.href.split("/", 1)
                if len(parts) == 2:
                    source_path = self.source_dir / parts[0] / parts[1]
            if not source_path.exists():
                self._warn("Manifest includes missing file: %s", item.href)
                continue
            with source_path.open("rb") as handle:
                data = handle.read()
            if source_path.suffix.lower() in {".xhtml", ".html"}:
                data = self._normalize_xhtml(data, item.href, identifier)
            archive.writestr(f"OEBPS/{item.href}", data)

    def _write_mimetype(self, archive: ZipFile) -> None:
        info = ZipInfo("mimetype")
        info.compress_type = ZIP_STORED
        archive.writestr(info, b"application/epub+zip")

    def _write_container_xml(self, archive: ZipFile) -> None:
        container_xml = (
            "<?xml version='1.0' encoding='utf-8'?>"
            "<container version='1.0' "
            "xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>"
            "<rootfiles>"
            "<rootfile full-path='OEBPS/content.opf' "
            "media-type='application/oebps-package+xml'/>"
            "</rootfiles>"
            "</container>"
        )
        archive.writestr("META-INF/container.xml", container_xml)

    def _render_cover_xhtml(
        self,
        *,
        identifier: str,
        image_href: str,
        image_alt: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> str:
        """Render a normalized cover.xhtml document.

        Parameters
        ----------
        identifier:
            The book identifier used to rewrite hosted asset URLs.
        image_href:
            Path to the cover image relative to the ``Images/`` directory or an
            absolute URL provided in the raw content.
        image_alt:
            Optional accessibility description for the cover image. If omitted,
            the alt text will fall back to a simple "Cover image" placeholder.
        width / height:
            Optional intrinsic image dimensions.
        """

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

        normalized_href = _rewrite_href(image_href)
        normalized_href = normalized_href.strip()
        if normalized_href.lower().startswith("images/"):
            normalized_href = normalized_href[7:]
        elif normalized_href != "":
            normalized_href = Path(normalized_href).name

        if not normalized_href:
            normalized_href = "cover.jpg"

        normalized_href = f"Images/{normalized_href}"

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

        cover_markup = (
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
        return cover_markup

    def _render_content_opf(
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
        nav_id: str,
        version_id: Optional[str] = None,
    ) -> bytes:
        ET.register_namespace("", "http://www.idpf.org/2007/opf")
        ET.register_namespace("dc", "http://purl.org/dc/elements/1.1/")

        package = ET.Element(
            "{http://www.idpf.org/2007/opf}package",
            attrib={
                "version": version_id or "3.0",
                "unique-identifier": unique_id,
            },
        )

        metadata_el = ET.SubElement(
            package,
            "{http://www.idpf.org/2007/opf}metadata",
            attrib={
                "xmlns:dc": "http://purl.org/dc/elements/1.1/",
                "xmlns:opf": "http://www.idpf.org/2007/opf",
            },
        )
        ET.SubElement(
            metadata_el,
            "{http://purl.org/dc/elements/1.1/}identifier",
            id=unique_id,
        ).text = identifier
        ET.SubElement(
            metadata_el,
            "{http://purl.org/dc/elements/1.1/}title",
        ).text = title
        ET.SubElement(
            metadata_el,
            "{http://purl.org/dc/elements/1.1/}language",
        ).text = language
        for author in authors:
            ET.SubElement(
                metadata_el,
                "{http://purl.org/dc/elements/1.1/}creator",
                attrib={"opf:file-as": author, "opf:role": "aut"},
            ).text = author
        for subject in categories:
            ET.SubElement(
                metadata_el,
                "{http://purl.org/dc/elements/1.1/}subject",
            ).text = subject
        for publisher in publishers:
            ET.SubElement(
                metadata_el,
                "{http://purl.org/dc/elements/1.1/}publisher",
            ).text = publisher
        if publication_date:
            ET.SubElement(
                metadata_el,
                "{http://purl.org/dc/elements/1.1/}date",
            ).text = publication_date
        if description_html:
            ET.SubElement(
                metadata_el,
                "dc:description",
            ).text = description_html

        cover_href = None
        manifest_el = ET.SubElement(
            package,
            "{http://www.idpf.org/2007/opf}manifest",
        )
        for item in manifest_items:
            attrib = {
                "id": item.item_id,
                "href": item.href,
                "media-type": item.media_type,
            }
            if item.properties:
                attrib["properties"] = item.properties
            ET.SubElement(
                manifest_el,
                "{http://www.idpf.org/2007/opf}item",
                attrib=attrib,
            )
            if item.item_id == "cover":
                cover_href = item.href

        manifest_lookup = {item.href: item.item_id for item in manifest_items}

        spine_el = ET.SubElement(
            package,
            "{http://www.idpf.org/2007/opf}spine",
        )

        for path in spine_items:
            try:
                href = path.relative_to(self.source_dir).as_posix()
            except ValueError:
                href = path.name
            item_id = manifest_lookup.get(href)
            if not item_id:
                self._warn("Spine item %s missing from manifest", href)
                continue
            ET.SubElement(
                spine_el,
                "{http://www.idpf.org/2007/opf}itemref",
                attrib={"idref": item_id},
            )

        ET.SubElement(
            metadata_el,
            "{http://www.idpf.org/2007/opf}meta",
            name="cover",
            content="False",
        )

        modified = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        meta_el = ET.SubElement(
            metadata_el,
            "{http://www.idpf.org/2007/opf}meta",
            attrib={"property": "dcterms:modified"},
        )
        meta_el.text = modified.isoformat().replace("+00:00", "Z")

        # <guide><reference href="cover.xhtml" title="Cover" type="cover" /></guide>
        if cover_href:
            guide_el = ET.SubElement(
                package,
                "{http://www.idpf.org/2007/opf}guide",
            )
            ET.SubElement(
                guide_el,
                "{http://www.idpf.org/2007/opf}reference",
                attrib={"href": cover_href, "title": "Cover", "type": "cover"},
            )

        return ET.tostring(package, encoding="utf-8", xml_declaration=True)

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

        title_text = Path(href).stem.replace("_", " ").strip()
        if not title_text:
            title_text = "Document"
        title_text = title_text.title()
        title_pattern = re.compile(r"<title>\s*</title>", re.IGNORECASE)
        text_body = title_pattern.sub(
            f"<title>{html.escape(title_text)}</title>", text_body, count=1
        )

        # Ensure decorative break spans actually render a visual break.
        break_span_pattern = re.compile(
            r"(<span\s+class=\"break\"[^>]*>)(.*?)(</span>)",
            re.IGNORECASE | re.DOTALL,
        )

        def _inject_break(match: re.Match[str]) -> str:
            opening, content, closing = match.groups()
            if "<br" in content.lower():
                return match.group(0)
            content = content or ""
            text_only = re.sub(r"<[^>]+>", "", content)
            text_only = html.unescape(text_only).strip()
            letters = [ch for ch in text_only if ch.isalpha()]
            is_all_upper = bool(letters) and all(ch.isupper() for ch in letters)
            injection = "<br />" if is_all_upper else ": "
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
                new_styles = styles.rstrip("; ") + "; text-align:center;"
                chop = slice(style_match.end(1), len(stripped))
                opening = opening[: style_match.start(1)] + new_styles + opening[chop]
            else:
                opening = opening.rstrip()
                if opening.endswith(">"):
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

        normalized = f"{xml_decl}\n{text_body.lstrip()}"
        return normalized.encode("utf-8")

    def _normalize_cover(self, text: str, identifier: str) -> bytes:
        """Transform arbitrary cover markup into a standard cover.xhtml body."""

        img_match = re.search(r"<img\b[^>]*>", text, re.IGNORECASE)
        if img_match:
            img_tag = img_match.group(0)
        else:
            img_tag = ""

        attr_pattern = re.compile(
            r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*')"
        )
        attrs = {
            name.lower(): value[1:-1] for name, value in attr_pattern.findall(img_tag)
        }

        src = attrs.get("src", "")
        alt = attrs.get("alt")
        width = attrs.get("width")
        height = attrs.get("height")

        def _to_int(value: Optional[str]) -> Optional[int]:
            if not value:
                return None
            value = value.strip()
            value = re.sub(r"[^0-9]", "", value)
            if not value:
                return None
            try:
                return int(value)
            except ValueError:
                return None

        width_int = _to_int(width)
        height_int = _to_int(height)

        markup = self._render_cover_xhtml(
            identifier=identifier,
            image_href=src,
            image_alt=alt,
            width=width_int,
            height=height_int,
        )
        return markup.encode("utf-8")

    def _manifest_id_from_href(self, href: str) -> str:
        base = Path(href).stem
        if "Images" in href:
            base = "img_" + base
        sanitized = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in base)
        while sanitized.startswith("_"):
            sanitized = sanitized[1:]
        if not sanitized:
            sanitized = "item"
        candidate = sanitized
        counter = 1
        while candidate in self._manifest_ids:
            counter += 1
            candidate = f"{sanitized}_{counter}"
        return candidate

    def _warn(self, message: str, *args: object) -> None:
        if args:
            message = message % args
        self.warnings.append(message)
