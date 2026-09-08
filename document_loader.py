"""Extensible document-to-text loading and extraction."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path

from errors import InputDocumentError

DocumentLoader = Callable[[Path], str]
_LOADERS: dict[str, DocumentLoader] = {}


def register_loader(*extensions: str) -> Callable[[DocumentLoader], DocumentLoader]:
    """Register a loader for one or more filename extensions.

    Third-party formats can be added without changing the orchestrator by calling
    this function during application startup.
    """

    normalized = tuple(
        extension.lower() if extension.startswith(".") else f".{extension.lower()}"
        for extension in extensions
    )

    def decorator(loader: DocumentLoader) -> DocumentLoader:
        for extension in normalized:
            _LOADERS[extension] = loader
        return loader

    return decorator


def _read_text(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InputDocumentError(f"Could not read input file '{path}': {exc}") from exc

    encodings = ["utf-8-sig"]
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.insert(0, "utf-16")
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise InputDocumentError(
        f"Input file '{path}' is not valid UTF-8 or BOM-marked UTF-16 text."
    )


@register_loader(".txt", ".md", ".markdown", ".tex", ".rst")
def _load_plain_text(path: Path) -> str:
    return _read_text(path)


@register_loader(".json")
def _load_json(path: Path) -> str:
    text = _read_text(path)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputDocumentError(
            f"Input JSON '{path}' is invalid at line {exc.lineno}, column {exc.colno}: "
            f"{exc.msg}"
        ) from exc
    return json.dumps(parsed, ensure_ascii=False, indent=2)


@register_loader(".csv")
def _load_csv(path: Path) -> str:
    text = _read_text(path)
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise InputDocumentError(f"Could not parse CSV input '{path}': {exc}") from exc
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(rows)
    return output.getvalue()


class _StructuredHTMLExtractor(HTMLParser):
    """Small dependency-free HTML-to-structured-text extractor."""

    _BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "footer",
        "header",
        "main",
        "nav",
        "p",
        "section",
        "table",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "br":
            self.parts.append("\n")
        elif tag in {"li", "dt"}:
            self.parts.append("\n- ")
        elif tag == "dd":
            self.parts.append("\n  ")
        elif tag in {"td", "th"}:
            self.parts.append("\t")
        elif len(tag) == 2 and tag.startswith("h") and tag[1].isdigit():
            self.parts.append(f"\n{'#' * int(tag[1])} ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if not self._ignored_depth and (tag in self._BLOCK_TAGS or tag in {"li", "dd"}):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        lines = [line.rstrip() for line in "".join(self.parts).splitlines()]
        compacted: list[str] = []
        previous_blank = True
        for line in lines:
            is_blank = not line.strip()
            if is_blank and previous_blank:
                continue
            compacted.append(line)
            previous_blank = is_blank
        return "\n".join(compacted).strip()


@register_loader(".html", ".htm")
def _load_html(path: Path) -> str:
    parser = _StructuredHTMLExtractor()
    try:
        parser.feed(_read_text(path))
        parser.close()
    except Exception as exc:
        raise InputDocumentError(f"Could not parse HTML input '{path}': {exc}") from exc
    return parser.text()


@register_loader(".pdf")
def _load_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise InputDocumentError(
            "PDF support requires 'pypdf'. Run: pip install -r requirements.txt"
        ) from exc

    try:
        reader = PdfReader(path)
        pages = []
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                page_text = page.extract_text(extraction_mode="layout") or ""
            except TypeError:
                # Compatibility with older pypdf releases that predate layout mode.
                page_text = page.extract_text() or ""
            pages.append(f"--- Page {page_number} ---\n{page_text.rstrip()}")
    except Exception as exc:
        raise InputDocumentError(f"Could not extract PDF input '{path}': {exc}") from exc
    return "\n\n".join(pages)


@register_loader(".docx")
def _load_docx(path: Path) -> str:
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:
        raise InputDocumentError(
            "DOCX support requires 'python-docx'. Run: pip install -r requirements.txt"
        ) from exc

    try:
        from docx.oxml.table import CT_Tbl
        from docx.oxml.text.paragraph import CT_P

        document = Document(path)
        parts: list[str] = []
        for element in document.element.body.iterchildren():
            block: Paragraph | Table
            if isinstance(element, CT_P):
                block = Paragraph(element, document)
            elif isinstance(element, CT_Tbl):
                block = Table(element, document)
            else:
                continue
            if isinstance(block, Paragraph):
                text = block.text
                if not text:
                    parts.append("")
                    continue
                style_name = block.style.name if block.style is not None else ""
                if style_name.startswith("Heading "):
                    try:
                        level = int(style_name.rsplit(" ", 1)[-1])
                    except ValueError:
                        level = 2
                    parts.append(f"{'#' * max(1, min(level, 6))} {text}")
                elif style_name.startswith("List"):
                    parts.append(f"- {text}")
                else:
                    parts.append(text)
            elif isinstance(block, Table):
                for row in block.rows:
                    parts.append("\t".join(cell.text for cell in row.cells))
                parts.append("")
    except Exception as exc:
        raise InputDocumentError(f"Could not extract DOCX input '{path}': {exc}") from exc
    return "\n".join(parts)


SUPPORTED_EXTENSIONS = frozenset(_LOADERS)


def load_document(path: Path) -> str:
    """Load a supported document and return normalized, structure-aware text."""

    path = Path(path)
    if not path.exists():
        raise InputDocumentError(f"Input file does not exist: {path}")
    if not path.is_file():
        raise InputDocumentError(f"Input path is not a file: {path}")

    extension = path.suffix.lower()
    try:
        loader = _LOADERS[extension]
    except KeyError as exc:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise InputDocumentError(
            f"Unsupported input format '{extension or '(none)'}'. Supported formats: {supported}."
        ) from exc

    text = loader(path).replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise InputDocumentError(f"No textual content could be extracted from '{path}'.")
    return text
