"""Structured, provenance-preserving scientific PDF parsing.

The canonical model is independent of any parser. A native pypdf path keeps
ScholarFlow usable without Docker, while an optional GROBID adapter can enrich
structure when a reviewed sidecar is configured.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable, Iterable, Iterator, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from .domain_queries import DomainQueryError

SCIENTIFIC_DOCUMENT_SCHEMA_VERSION = 2
DEFAULT_GROBID_BASE_URL = "http://127.0.0.1:8070"
HEADING_NAMES = {
    "abstract",
    "acknowledgement",
    "acknowledgements",
    "background",
    "conclusion",
    "conclusions",
    "discussion",
    "introduction",
    "materials and methods",
    "method",
    "methods",
    "references",
    "results",
    "results and discussion",
    "supplementary information",
}
CAPTION_RE = re.compile(
    r"^(?:fig(?:ure)?\.?|table)\s*(?:s?\d+|[ivxlcdm]+)\b",
    re.IGNORECASE,
)
NUMBERED_HEADING_RE = re.compile(r"^(?:\d+(?:\.\d+){0,4})[\s.)-]+(.+)$")
REFERENCE_START_RE = re.compile(r"^\s*(?:\[\d+\]|\d+[.)])\s+")
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
NUMERIC_CITATION_RE = re.compile(
    r"\[(?P<numbers>\d+(?:\s*(?:,|;|-|–|—)\s*\d+)*)\]"
)
PARENTHETICAL_CITATION_RE = re.compile(r"\((?P<body>[^()\n]{3,360})\)")
AUTHOR_YEAR_TRAILER_RE = re.compile(
    r"(?P<label>[A-ZÀ-ÖØ-Þ][^,;()]{1,100}?)\s*,\s*"
    r"(?P<year>(?:19|20)\d{2}[a-z]?)"
    r"(?:\s*,\s*(?:p{1,2}\.?\s*)?\d[^;()]*)?$"
)


@dataclass(frozen=True)
class ScientificBlock:
    block_id: str
    kind: str
    text: str
    page_start: int | None
    page_end: int | None
    section_path: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["section_path"] = list(self.section_path)
        return payload


@dataclass(frozen=True)
class ScientificReference:
    reference_id: str
    raw_text: str
    page: int | None = None
    doi: str = ""
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ScientificCitation:
    citation_id: str
    block_id: str
    reference_ids: tuple[str, ...]
    raw_text: str
    page: int | None = None
    char_start: int | None = None
    char_end: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reference_ids"] = list(self.reference_ids)
        return payload


@dataclass(frozen=True)
class ScientificDocument:
    document_id: str
    title: str
    content_hash: str
    page_count: int
    parser_name: str
    parser_version: str
    blocks: tuple[ScientificBlock, ...]
    authors: tuple[str, ...] = ()
    references: tuple[ScientificReference, ...] = ()
    citations: tuple[ScientificCitation, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    schema_version: int = SCIENTIFIC_DOCUMENT_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ScientificDocument":
        """Restore the canonical parsed document without running a parser."""
        try:
            if payload.get("schema_version") != SCIENTIFIC_DOCUMENT_SCHEMA_VERSION:
                raise ValueError("unsupported scientific document schema")
            parser = payload.get("parser") or {}
            blocks = tuple(ScientificBlock(
                block_id=str(item["block_id"]), kind=str(item["kind"]), text=str(item["text"]),
                page_start=item.get("page_start"), page_end=item.get("page_end"),
                section_path=tuple(item.get("section_path") or []), metadata=dict(item.get("metadata") or {}),
            ) for item in payload["blocks"])
            return cls(
                document_id=str(payload["document_id"]), title=str(payload["title"]),
                content_hash=str(payload["content_hash"]), page_count=int(payload["page_count"]),
                parser_name=str(parser["name"]), parser_version=str(parser["version"]), blocks=blocks,
                authors=tuple(payload.get("authors") or []),
                references=tuple(ScientificReference(**item) for item in payload.get("references", [])),
                citations=tuple(ScientificCitation(**{**item, "reference_ids": tuple(item.get("reference_ids") or [])}) for item in payload.get("citations", [])),
                metadata=dict(payload.get("metadata") or {}), warnings=tuple(payload.get("warnings") or []),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainQueryError("knowledge_scientific_document_invalid", "cached scientific document is invalid") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "document_id": self.document_id,
            "title": self.title,
            "authors": list(self.authors),
            "content_hash": self.content_hash,
            "page_count": self.page_count,
            "parser": {
                "name": self.parser_name,
                "version": self.parser_version,
            },
            "blocks": [block.to_dict() for block in self.blocks],
            "references": [reference.to_dict() for reference in self.references],
            "citations": [citation.to_dict() for citation in self.citations],
            "metadata": dict(self.metadata),
            "warnings": list(self.warnings),
        }

    def to_index_markdown(self) -> str:
        """Render citation-friendly text without exposing the absolute PDF path."""

        lines = [
            f"# {self.title}",
            "",
            f"- Scientific document: {self.document_id}",
            f"- Source content hash: {self.content_hash}",
            f"- Pages: {self.page_count}",
            f"- Parser: {self.parser_name}/{self.parser_version}",
        ]
        if self.authors:
            lines.append(f"- Authors: {', '.join(self.authors)}")
        lines.append("")
        citations_by_block: dict[str, list[str]] = {}
        for citation in self.citations:
            citations_by_block.setdefault(citation.block_id, []).extend(
                citation.reference_ids
            )
        for block in self.blocks:
            if block.kind == "reference_entry":
                continue
            if block.page_start is None and block.page_end is None:
                page_label = "?"
            elif block.page_start == block.page_end:
                page_label = str(block.page_start)
            else:
                page_label = f"{block.page_start or '?'}-{block.page_end or '?'}"
            section_label = " > ".join(block.section_path) or "Document"
            encoded_section = json.dumps(
                section_label,
                ensure_ascii=False,
            ).replace("--", "- -")
            citation_ids = ",".join(
                dict.fromkeys(citations_by_block.get(block.block_id, ()))
            )
            citation_attribute = (
                f" citations={citation_ids}" if citation_ids else ""
            )
            lines.extend(
                [
                    (
                        "<!-- sf-anchor "
                        f"block={block.block_id} page={page_label} "
                        f"section={encoded_section} "
                        f"kind={block.kind}{citation_attribute} -->"
                    ),
                    _render_block_markdown(block),
                    "",
                ]
            )
        if self.references:
            lines.extend(["## References", ""])
            for reference in self.references:
                page = reference.page if reference.page is not None else "?"
                lines.extend(
                    [
                        (
                            "<!-- sf-reference "
                            f"id={reference.reference_id} page={page} -->"
                        ),
                        f"- [{reference.reference_id}] {reference.raw_text}",
                        "",
                    ]
                )
        return "\n".join(lines).strip()

    def with_warning(self, warning: str) -> "ScientificDocument":
        return replace(self, warnings=(*self.warnings, str(warning)))

    @property
    def structure_summary(self) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for block in self.blocks:
            kinds[block.kind] = kinds.get(block.kind, 0) + 1
        return {
            "schema_version": self.schema_version,
            "document_id": self.document_id,
            "page_count": self.page_count,
            "block_count": len(self.blocks),
            "reference_count": len(self.references),
            "citation_count": len(self.citations),
            "block_kinds": kinds,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "warnings": list(self.warnings),
        }


class ScientificPdfParser(Protocol):
    name: str

    def parse(self, path: Path) -> ScientificDocument: ...


class NativeScientificPdfParser:
    """Dependency-light PDF parser with deterministic page and section anchors."""

    name = "pypdf-native"

    def __init__(self, reader_factory: Callable[[str], Any] | None = None):
        self._reader_factory = reader_factory

    def parse(self, path: Path) -> ScientificDocument:
        pdf_path = _validated_pdf_path(path)
        raw = pdf_path.read_bytes()
        content_digest = sha256(raw).hexdigest()
        reader_factory = self._reader_factory
        parser_version = "injected"
        if reader_factory is None:
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise DomainQueryError(
                    "knowledge_pdf_extractor_unavailable",
                    "Scientific PDF ingestion requires the optional pypdf package",
                ) from exc
            reader_factory = PdfReader
            parser_version = _package_version("pypdf")
        try:
            reader = reader_factory(str(pdf_path))
            pages = [_extract_page_text(page) for page in reader.pages]
        except DomainQueryError:
            raise
        except Exception as exc:
            raise DomainQueryError(
                "knowledge_pdf_parse_failed",
                f"could not parse scientific PDF: {pdf_path.name}",
            ) from exc
        if not pages:
            raise DomainQueryError(
                "knowledge_pdf_parse_failed",
                "scientific PDF contains no pages",
            )

        metadata = _reader_metadata(reader)
        repeated_margins = _repeated_margin_lines(pages)
        blocks, references, warnings = _native_blocks(
            pages,
            repeated_margins=repeated_margins,
            excluded_front_matter={
                _normalize_space(str(metadata.get("title") or "")),
                _normalize_space(str(metadata.get("author") or "")),
            },
        )
        if not any(block.text.strip() for block in blocks):
            raise DomainQueryError(
                "knowledge_pdf_text_unavailable",
                "scientific PDF has no extractable text; OCR is required",
                details={"file_name": pdf_path.name},
            )
        title = _choose_title(
            metadata_title=str(metadata.get("title") or ""),
            blocks=blocks,
            file_stem=pdf_path.stem,
        )
        authors = tuple(
            item.strip()
            for item in re.split(r"\s*(?:;|\band\b)\s*", str(metadata.get("author") or ""))
            if item.strip()
        )
        citations = _native_citations(blocks, references, warnings)
        return ScientificDocument(
            document_id=f"pdf-{content_digest[:24]}",
            title=title[:300],
            authors=authors,
            content_hash=f"sha256:{content_digest}",
            page_count=len(pages),
            parser_name=self.name,
            parser_version=parser_version,
            blocks=tuple(blocks),
            references=tuple(references),
            citations=tuple(citations),
            metadata={
                "file_name": pdf_path.name,
                "subject": str(metadata.get("subject") or "")[:500],
            },
            warnings=tuple(warnings),
        )


class GrobidScientificPdfParser:
    """Narrow GROBID `processFulltextDocument` adapter returning our model."""

    name = "grobid"

    def __init__(
        self,
        base_url: str = DEFAULT_GROBID_BASE_URL,
        *,
        timeout_seconds: float = 120.0,
        allow_remote: bool = False,
        opener: Callable[..., Any] = urlopen,
    ):
        self.base_url = _validate_grobid_url(base_url, allow_remote=allow_remote)
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0 or self.timeout_seconds > 900:
            raise DomainQueryError(
                "knowledge_pdf_parser_configuration_error",
                "GROBID timeout must be between 0 and 900 seconds",
            )
        self._opener = opener

    def parse(self, path: Path) -> ScientificDocument:
        pdf_path = _validated_pdf_path(path)
        raw = pdf_path.read_bytes()
        content_digest = sha256(raw).hexdigest()
        boundary = f"scholarflow-{content_digest[:24]}"
        body = _multipart_pdf_body(boundary, pdf_path.name, raw)
        request = Request(
            f"{self.base_url}/api/processFulltextDocument",
            data=body,
            headers={
                "Accept": "application/xml",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                xml_bytes = response.read()
                parser_version = str(
                    response.headers.get("Grobid-Version", "service")
                    if getattr(response, "headers", None)
                    else "service"
                )
        except HTTPError as exc:
            raise DomainQueryError(
                "knowledge_grobid_rejected",
                f"GROBID returned HTTP {exc.code}",
                details={"status": exc.code},
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise DomainQueryError(
                "knowledge_grobid_unavailable",
                "GROBID is unavailable or timed out",
            ) from exc
        return _document_from_tei(
            xml_bytes,
            file_name=pdf_path.name,
            content_digest=content_digest,
            parser_version=parser_version,
        )


class TesseractScientificPdfParser:
    """Optional OCR parser for image-only PDFs with no shell invocation."""

    name = "tesseract-ocr"

    def __init__(
        self,
        executable: str = "tesseract",
        *,
        languages: str = "eng",
        dpi: int = 200,
        max_pages: int = 200,
        page_timeout_seconds: float = 60,
        renderer: Callable[[Path, int, int], Iterable[bytes]] | None = None,
        ocr_runner: Callable[[bytes, str, float], str] | None = None,
    ):
        self.executable = str(executable or "tesseract").strip()
        self.languages = str(languages or "eng").strip()
        self.dpi = int(dpi)
        self.max_pages = int(max_pages)
        self.page_timeout_seconds = float(page_timeout_seconds)
        if self.dpi < 100 or self.dpi > 600:
            raise DomainQueryError(
                "knowledge_pdf_parser_configuration_error",
                "OCR DPI must be between 100 and 600",
            )
        if self.max_pages < 1 or self.max_pages > 2000:
            raise DomainQueryError(
                "knowledge_pdf_parser_configuration_error",
                "OCR max pages must be between 1 and 2000",
            )
        if self.page_timeout_seconds <= 0 or self.page_timeout_seconds > 600:
            raise DomainQueryError(
                "knowledge_pdf_parser_configuration_error",
                "OCR page timeout must be between 0 and 600 seconds",
            )
        self._renderer = renderer
        self._ocr_runner = ocr_runner

    def parse(self, path: Path) -> ScientificDocument:
        pdf_path = _validated_pdf_path(path)
        raw = pdf_path.read_bytes()
        content_digest = sha256(raw).hexdigest()
        images = (
            self._renderer(pdf_path, self.dpi, self.max_pages)
            if self._renderer is not None
            else self._render_pages(pdf_path)
        )
        pages: list[str] = []
        warnings: list[str] = []
        for index, image in enumerate(images, start=1):
            try:
                text = (
                    self._ocr_runner(
                        image,
                        self.languages,
                        self.page_timeout_seconds,
                    )
                    if self._ocr_runner is not None
                    else self._run_tesseract(image)
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise DomainQueryError(
                    "knowledge_pdf_ocr_failed",
                    f"OCR failed on page {index}",
                    details={"page": index},
                ) from exc
            normalized = str(text or "")
            if not normalized.strip():
                warnings.append(f"ocr_blank_page:{index}")
            pages.append(normalized)
        if not pages:
            raise DomainQueryError(
                "knowledge_pdf_ocr_failed",
                "OCR renderer produced no PDF pages",
            )
        repeated_margins = _repeated_margin_lines(pages)
        blocks, references, native_warnings = _native_blocks(
            pages,
            repeated_margins=repeated_margins,
            excluded_front_matter=set(),
        )
        if not any(block.text.strip() for block in blocks):
            raise DomainQueryError(
                "knowledge_pdf_text_unavailable",
                "OCR produced no indexable text",
            )
        title = _choose_title(
            metadata_title="",
            blocks=blocks,
            file_stem=pdf_path.stem,
        )
        citations = _native_citations(blocks, references, native_warnings)
        return ScientificDocument(
            document_id=f"pdf-{content_digest[:24]}",
            title=title,
            content_hash=f"sha256:{content_digest}",
            page_count=len(pages),
            parser_name=self.name,
            parser_version=self._version(),
            blocks=tuple(blocks),
            references=tuple(references),
            citations=tuple(citations),
            metadata={
                "file_name": pdf_path.name,
                "ocr_languages": self.languages,
                "ocr_dpi": self.dpi,
            },
            warnings=tuple((*native_warnings, *warnings)),
        )

    def _render_pages(self, pdf_path: Path) -> Iterator[bytes]:
        try:
            import fitz
        except ImportError as exc:
            raise DomainQueryError(
                "knowledge_pdf_ocr_unavailable",
                "OCR requires the optional PyMuPDF package",
            ) from exc
        try:
            with fitz.open(pdf_path) as document:
                if document.page_count > self.max_pages:
                    raise DomainQueryError(
                        "knowledge_pdf_ocr_page_limit",
                        "PDF exceeds the configured OCR page limit",
                        details={
                            "pages": document.page_count,
                            "maximum": self.max_pages,
                        },
                    )
                scale = self.dpi / 72
                for page in document:
                    yield page.get_pixmap(
                        matrix=fitz.Matrix(scale, scale),
                        alpha=False,
                    ).tobytes("png")
        except DomainQueryError:
            raise
        except Exception as exc:
            raise DomainQueryError(
                "knowledge_pdf_ocr_failed",
                "could not render PDF pages for OCR",
            ) from exc

    def _run_tesseract(self, image: bytes) -> str:
        executable = shutil.which(self.executable)
        if executable is None:
            raise DomainQueryError(
                "knowledge_pdf_ocr_unavailable",
                "Tesseract executable is not configured or available",
            )
        completed = subprocess.run(
            [
                executable,
                "stdin",
                "stdout",
                "-l",
                self.languages,
                "--psm",
                "3",
            ],
            input=image,
            capture_output=True,
            timeout=self.page_timeout_seconds,
            check=False,
            env=_tesseract_subprocess_environment(executable),
        )
        if completed.returncode != 0:
            raise DomainQueryError(
                "knowledge_pdf_ocr_failed",
                "Tesseract returned a non-zero exit status",
                details={"return_code": completed.returncode},
            )
        return completed.stdout.decode("utf-8", errors="replace")

    def _version(self) -> str:
        if self._ocr_runner is not None:
            return "injected"
        executable = shutil.which(self.executable)
        if executable is None:
            return "unavailable"
        try:
            result = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                timeout=10,
                check=False,
                text=True,
                env=_tesseract_subprocess_environment(executable),
            )
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        match = re.search(r"\d+(?:\.\d+){1,3}", result.stdout or result.stderr)
        return match.group(0) if match else "unknown"


def _tesseract_subprocess_environment(executable: str) -> dict[str, str]:
    """Make isolated Windows/Conda Tesseract runtimes self-contained."""

    environment = dict(os.environ)
    executable_path = Path(executable).resolve()
    runtime_dirs = [executable_path.parent]
    try:
        prefix = executable_path.parents[2]
    except IndexError:
        prefix = executable_path.parent
    if (
        executable_path.parent.name.casefold() == "bin"
        and executable_path.parent.parent.name.casefold() == "library"
    ):
        runtime_dirs.extend(
            (
                prefix,
                prefix / "Scripts",
                prefix / "Library" / "bin",
            )
        )
        tessdata = prefix / "share" / "tessdata"
        if tessdata.is_dir() and not environment.get("TESSDATA_PREFIX"):
            environment["TESSDATA_PREFIX"] = str(tessdata)
    existing = environment.get("PATH", "")
    environment["PATH"] = os.pathsep.join(
        (
            *(str(path) for path in dict.fromkeys(runtime_dirs)),
            existing,
        )
    )
    return environment


class ScientificPdfParserRouter:
    """Select native/GROBID parsing without making the sidecar mandatory."""

    def __init__(
        self,
        *,
        mode: str = "auto",
        native: ScientificPdfParser | None = None,
        grobid: ScientificPdfParser | None = None,
        ocr: ScientificPdfParser | None = None,
    ):
        normalized = str(mode or "auto").strip().casefold()
        if normalized not in {"auto", "native", "grobid", "ocr", "pymupdf-blocks"}:
            raise DomainQueryError(
                "knowledge_pdf_parser_configuration_error",
                "PDF parser mode must be auto, native, grobid, or ocr",
            )
        self.mode = normalized
        self.native = native or NativeScientificPdfParser()
        self.grobid = grobid
        self.ocr = ocr

    def parse(self, path: Path) -> ScientificDocument:
        if self.mode == "pymupdf-blocks":
            return PyMuPdfBlockParser().parse(path)
        if self.mode == "native":
            return self.native.parse(path)
        if self.mode == "grobid":
            if self.grobid is None:
                raise DomainQueryError(
                    "knowledge_grobid_unconfigured",
                    "GROBID mode requires SCHOLARFLOW_GROBID_BASE_URL",
                )
            return self.grobid.parse(path)
        if self.mode == "ocr":
            if self.ocr is None:
                raise DomainQueryError(
                    "knowledge_pdf_ocr_unconfigured",
                    "OCR mode requires SCHOLARFLOW_TESSERACT_EXE",
                )
            return self.ocr.parse(path)
        grobid_error: DomainQueryError | None = None
        if self.grobid is not None:
            try:
                return self.grobid.parse(path)
            except DomainQueryError as exc:
                grobid_error = exc
        try:
            document = self.native.parse(path)
            return (
                document.with_warning(f"grobid_fallback:{grobid_error.code}")
                if grobid_error is not None
                else document
            )
        except DomainQueryError as native_error:
            if (
                self.ocr is None
                or native_error.code != "knowledge_pdf_text_unavailable"
            ):
                raise
            document = self.ocr.parse(path).with_warning(
                f"native_fallback:{native_error.code}"
            )
            return (
                document.with_warning(f"grobid_fallback:{grobid_error.code}")
                if grobid_error is not None
                else document
            )


def build_scientific_pdf_parser() -> ScientificPdfParser:
    mode = os.environ.get("SCHOLARFLOW_PDF_PARSER", "auto")
    grobid_url = os.environ.get("SCHOLARFLOW_GROBID_BASE_URL", "").strip()
    grobid: ScientificPdfParser | None = None
    if grobid_url:
        try:
            timeout_seconds = float(
                os.environ.get("SCHOLARFLOW_GROBID_TIMEOUT_SECONDS", "120")
            )
        except ValueError as exc:
            raise DomainQueryError(
                "knowledge_pdf_parser_configuration_error",
                "SCHOLARFLOW_GROBID_TIMEOUT_SECONDS must be numeric",
            ) from exc
        grobid = GrobidScientificPdfParser(
            grobid_url,
            timeout_seconds=timeout_seconds,
            allow_remote=_truthy(
                os.environ.get("SCHOLARFLOW_GROBID_ALLOW_REMOTE", "")
            ),
        )
    tesseract_executable = os.environ.get(
        "SCHOLARFLOW_TESSERACT_EXE",
        "",
    ).strip()
    ocr: ScientificPdfParser | None = None
    if tesseract_executable or str(mode).strip().casefold() == "ocr":
        try:
            ocr_dpi = int(os.environ.get("SCHOLARFLOW_OCR_DPI", "200"))
            ocr_max_pages = int(
                os.environ.get("SCHOLARFLOW_OCR_MAX_PAGES", "200")
            )
            ocr_timeout = float(
                os.environ.get("SCHOLARFLOW_OCR_PAGE_TIMEOUT_SECONDS", "60")
            )
        except ValueError as exc:
            raise DomainQueryError(
                "knowledge_pdf_parser_configuration_error",
                "OCR DPI, page limit and timeout must be numeric",
            ) from exc
        ocr = TesseractScientificPdfParser(
            tesseract_executable or "tesseract",
            languages=os.environ.get(
                "SCHOLARFLOW_OCR_LANGUAGES",
                "eng",
            ),
            dpi=ocr_dpi,
            max_pages=ocr_max_pages,
            page_timeout_seconds=ocr_timeout,
        )
    return ScientificPdfParserRouter(mode=mode, grobid=grobid, ocr=ocr)


class PyMuPdfBlockParser:
    """Preserve library text blocks and boxes; no automatic column-order claims."""
    name = "pymupdf-blocks"
    def parse(self, path: Path) -> ScientificDocument:
        import fitz
        pdf_path = _validated_pdf_path(path)
        raw = pdf_path.read_bytes();digest = sha256(raw).hexdigest();blocks=[]
        with fitz.open(pdf_path) as doc:
            if doc.needs_pass: raise DomainQueryError("knowledge_pdf_source_invalid","Encrypted PDF requires host handling")
            if doc.page_count > 500: raise DomainQueryError("resource_limit","Split PDF above 500 pages before block parsing")
            for page_index,page in enumerate(doc,1):
                for block_index,b in enumerate(page.get_text("blocks"),1):
                    if b[6] != 0 or not b[4].strip():continue
                    text=b[4].replace("\r\n","\n").replace("\x00","").strip()
                    block_id=f"page-{page_index:05d}-block-{block_index:04d}-"+sha256(text.encode()).hexdigest()[:10]
                    blocks.append(ScientificBlock(block_id,"paragraph",text,page_index,page_index,metadata={"bbox":list(b[:4]),"layout":"library text block; full reading order requires review"}))
            if not blocks:raise DomainQueryError("knowledge_pdf_text_unavailable","No text blocks; OCR not run")
            return ScientificDocument("pdf-"+digest[:24],doc.metadata.get("title") or pdf_path.stem,"sha256:"+digest,doc.page_count,self.name,fitz.VersionBind+"-block-v1",tuple(blocks),warnings=("layout_order_requires_review","figures_tables_and_formulae_not_verified"))


def parse_scientific_pdf(
    path: Path,
    *,
    parser: ScientificPdfParser | None = None,
) -> ScientificDocument:
    return (parser or build_scientific_pdf_parser()).parse(Path(path))


def _validated_pdf_path(path: Path) -> Path:
    resolved = Path(path).resolve()
    if not resolved.is_file() or resolved.suffix.casefold() != ".pdf":
        raise DomainQueryError(
            "knowledge_pdf_source_invalid",
            f"scientific PDF source is invalid: {resolved}",
        )
    return resolved


def _extract_page_text(page: Any) -> str:
    # /Contents is optional for a genuinely empty PDF page. Some pypdf
    # versions' layout path indexes it directly and raises instead of returning empty text.
    if isinstance(page, dict) and "/Contents" not in page:
        return ""
    try:
        text = page.extract_text(extraction_mode="layout")
    except TypeError:
        text = page.extract_text()
    except Exception as exc:
        raise DomainQueryError(
            "knowledge_pdf_parse_failed",
            "PDF page text extraction failed",
        ) from exc
    return str(text or "").replace("\x00", "").replace("\r\n", "\n")


def _reader_metadata(reader: Any) -> dict[str, Any]:
    raw = getattr(reader, "metadata", None)
    if raw is None:
        return {}
    getter = getattr(raw, "get", None)
    if not callable(getter):
        return {}
    return {
        "title": getter("/Title") or getter("title") or "",
        "author": getter("/Author") or getter("author") or "",
        "subject": getter("/Subject") or getter("subject") or "",
    }


def _repeated_margin_lines(pages: list[str]) -> set[str]:
    if len(pages) < 3:
        return set()
    counts: dict[str, int] = {}
    for text in pages:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in {*(lines[:2]), *(lines[-2:])}:
            normalized = _margin_key(line)
            if 2 <= len(normalized) <= 160:
                counts[normalized] = counts.get(normalized, 0) + 1
    threshold = max(3, math.ceil(len(pages) * 0.6))
    return {line for line, count in counts.items() if count >= threshold}


def _native_blocks(
    pages: list[str],
    *,
    repeated_margins: set[str],
    excluded_front_matter: set[str] | None = None,
) -> tuple[list[ScientificBlock], list[ScientificReference], list[str]]:
    blocks: list[ScientificBlock] = []
    references: list[ScientificReference] = []
    warnings: list[str] = []
    section_path: tuple[str, ...] = ()
    in_references = False
    ordinal = 0
    for page_number, page_text in enumerate(pages, start=1):
        lines = [
            _normalize_space(line)
            for line in page_text.splitlines()
        ]
        lines = [
            line
            for line in lines
            if line
            and _margin_key(line) not in repeated_margins
            and not (
                page_number == 1
                and line in (excluded_front_matter or set())
            )
        ]
        if not lines:
            warnings.append(f"page_without_extractable_text:{page_number}")
            continue
        paragraph: list[str] = []

        def flush_paragraph() -> None:
            nonlocal ordinal
            if not paragraph:
                return
            text = _normalize_space(" ".join(paragraph))
            paragraph.clear()
            if not text:
                return
            ordinal += 1
            kind = "reference_entry" if in_references else "paragraph"
            block = _make_block(
                ordinal,
                kind,
                text,
                page_number,
                section_path,
            )
            blocks.append(block)
            if in_references:
                references.append(
                    ScientificReference(
                        reference_id=f"ref-{len(references) + 1:04d}",
                        raw_text=text,
                        page=page_number,
                        doi=_first_doi(text),
                    )
                )

        for line in lines:
            if in_references:
                if REFERENCE_START_RE.match(line):
                    flush_paragraph()
                paragraph.append(line)
                continue
            if _is_heading(line):
                flush_paragraph()
                heading = _clean_heading(line)
                section_path = _section_path(section_path, heading)
                in_references = heading.casefold() in {
                    "references",
                    "bibliography",
                }
                ordinal += 1
                blocks.append(
                    _make_block(
                        ordinal,
                        "heading",
                        heading,
                        page_number,
                        section_path,
                    )
                )
                continue
            if CAPTION_RE.match(line):
                flush_paragraph()
                ordinal += 1
                blocks.append(
                    _make_block(
                        ordinal,
                        (
                            "table_caption"
                            if line.casefold().startswith("table")
                            else "figure_caption"
                        ),
                        line,
                        page_number,
                        section_path,
                    )
                )
                continue
            paragraph.append(line)
        flush_paragraph()
    return blocks, references, warnings


def _make_block(
    ordinal: int,
    kind: str,
    text: str,
    page: int | None,
    section_path: tuple[str, ...],
    *,
    metadata: dict[str, Any] | None = None,
) -> ScientificBlock:
    digest = sha256(
        f"{ordinal}\0{kind}\0{page}\0{text}".encode("utf-8")
    ).hexdigest()[:10]
    return ScientificBlock(
        block_id=f"block-{ordinal:05d}-{digest}",
        kind=kind,
        text=text,
        page_start=page,
        page_end=page,
        section_path=section_path,
        metadata=dict(metadata or {}),
    )


def _numeric_citations(
    blocks: Iterable[ScientificBlock],
    references: list[ScientificReference],
) -> list[ScientificCitation]:
    """Resolve high-confidence bracketed numeric citations by bibliography order."""

    citations: list[ScientificCitation] = []
    reference_ids = [reference.reference_id for reference in references]
    for block in blocks:
        if block.kind == "reference_entry":
            continue
        for match in NUMERIC_CITATION_RE.finditer(block.text):
            ordinals = _citation_ordinals(match.group("numbers"))
            resolved = tuple(
                reference_ids[ordinal - 1]
                for ordinal in ordinals
                if 1 <= ordinal <= len(reference_ids)
            )
            if not resolved:
                continue
            digest = sha256(
                (
                    f"{block.block_id}\0{match.start()}\0"
                    f"{','.join(resolved)}"
                ).encode("utf-8")
            ).hexdigest()[:10]
            citations.append(
                ScientificCitation(
                    citation_id=f"cite-{len(citations) + 1:05d}-{digest}",
                    block_id=block.block_id,
                    reference_ids=resolved,
                    raw_text=match.group(0),
                    page=block.page_start,
                    char_start=match.start(),
                    char_end=match.end(),
                )
            )
    return citations


def _native_citations(
    blocks: list[ScientificBlock],
    references: list[ScientificReference],
    warnings: list[str],
) -> list[ScientificCitation]:
    """Build conservative native citations without inventing bibliography data."""

    if references:
        return _numeric_citations(blocks, references)
    discovered, citations = _author_year_mentions(blocks)
    if discovered:
        references.extend(discovered)
        warnings.append("bibliography_unstructured:author_year_mentions_only")
    return citations


def _author_year_mentions(
    blocks: Iterable[ScientificBlock],
) -> tuple[list[ScientificReference], list[ScientificCitation]]:
    """Recover explicit author-year mentions when a bibliography cannot be split.

    Multi-column PDFs often interleave the ``References`` heading with body text.
    In that degraded case we retain only author-year strings visibly present in
    parenthetical citations. They are useful graph nodes, but are intentionally
    not presented as complete reference-manager records.
    """

    references: list[ScientificReference] = []
    citations: list[ScientificCitation] = []
    ids_by_key: dict[str, str] = {}
    for block in blocks:
        if block.kind in {"reference_entry", "heading"}:
            continue
        for parenthetical in PARENTHETICAL_CITATION_RE.finditer(block.text):
            resolved: list[str] = []
            for segment in parenthetical.group("body").split(";"):
                match = AUTHOR_YEAR_TRAILER_RE.search(_normalize_space(segment))
                if match is None:
                    continue
                label = _normalize_space(match.group("label"))
                year = match.group("year")
                if not 1 <= len(label.split()) <= 15:
                    continue
                key = f"{label.casefold()}\0{year.casefold()}"
                reference_id = ids_by_key.get(key)
                if reference_id is None:
                    if len(references) >= 300:
                        continue
                    reference_id = f"ref-{len(references) + 1:04d}"
                    ids_by_key[key] = reference_id
                    references.append(
                        ScientificReference(
                            reference_id=reference_id,
                            raw_text=f"{label} ({year})",
                            page=block.page_start,
                        )
                    )
                resolved.append(reference_id)
            unique = tuple(dict.fromkeys(resolved))
            if not unique or len(citations) >= 2000:
                continue
            digest = sha256(
                (
                    f"{block.block_id}\0{parenthetical.start()}\0"
                    f"{','.join(unique)}"
                ).encode("utf-8")
            ).hexdigest()[:10]
            citations.append(
                ScientificCitation(
                    citation_id=f"cite-{len(citations) + 1:05d}-{digest}",
                    block_id=block.block_id,
                    reference_ids=unique,
                    raw_text=parenthetical.group(0),
                    page=block.page_start,
                    char_start=parenthetical.start(),
                    char_end=parenthetical.end(),
                )
            )
    return references, citations


def _citation_ordinals(value: str) -> tuple[int, ...]:
    ordinals: list[int] = []
    parts = re.split(r"\s*[,;]\s*", value)
    for part in parts:
        range_match = re.fullmatch(r"(\d+)\s*[-–—]\s*(\d+)", part)
        if range_match:
            start, end = (int(item) for item in range_match.groups())
            if start <= end and end - start <= 50:
                ordinals.extend(range(start, end + 1))
            continue
        if part.isdigit():
            ordinals.append(int(part))
    return tuple(dict.fromkeys(ordinals))


def _is_heading(line: str) -> bool:
    normalized = line.strip().rstrip(":")
    folded = normalized.casefold()
    if folded in HEADING_NAMES or folded == "bibliography":
        return True
    numbered = NUMBERED_HEADING_RE.match(normalized)
    if numbered and 1 <= len(numbered.group(1).split()) <= 14:
        return True
    letters = [char for char in normalized if char.isalpha()]
    return (
        2 <= len(normalized.split()) <= 10
        and len(normalized) <= 100
        and bool(letters)
        and all(char.isupper() for char in letters)
    )


def _clean_heading(line: str) -> str:
    match = NUMBERED_HEADING_RE.match(line.strip())
    return _normalize_space(match.group(1) if match else line).rstrip(":")


def _section_path(
    current: tuple[str, ...],
    heading: str,
) -> tuple[str, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)", heading)
    if not match:
        return (heading,)
    depth = match.group(1).count(".") + 1
    return (*current[: max(0, depth - 1)], heading)


def _infer_title(
    blocks: Iterable[ScientificBlock],
    *,
    fallback: str,
) -> str:
    for block in blocks:
        if block.kind in {"paragraph", "heading"}:
            candidate = block.text.strip()
            if candidate and candidate.casefold() not in HEADING_NAMES:
                return candidate[:300]
    return fallback


def _choose_title(
    *,
    metadata_title: str,
    blocks: Iterable[ScientificBlock],
    file_stem: str,
) -> str:
    """Choose the least artifact-like title from metadata, content and filename."""

    inferred = _infer_title(blocks, fallback="")
    filename_title = _title_from_file_stem(file_stem)
    candidates = [
        (metadata_title, 8),
        (inferred, 0),
        (filename_title, 4),
        (file_stem, -6),
    ]
    scored = [
        (
            _title_quality(candidate) + source_bonus,
            -index,
            _normalize_space(candidate),
        )
        for index, (candidate, source_bonus) in enumerate(candidates)
        if _normalize_space(candidate)
    ]
    _score, _priority, selected = max(scored, default=(0, 0, file_stem))
    return selected[:300]


def _title_from_file_stem(value: str) -> str:
    text = _normalize_space(value.replace("_", " "))
    parts = [part.strip() for part in re.split(r"\s+-\s+", text) if part.strip()]
    if len(parts) >= 3 and re.fullmatch(r"(?:19|20)\d{2}", parts[1]):
        return " - ".join(parts[2:])
    if len(parts) >= 2 and (
        len(parts[0]) < 80
        and re.search(r"(?:\bet al\.?|\band\b|等|和)", parts[0], re.IGNORECASE)
    ):
        return " - ".join(parts[1:])
    return text


def _title_quality(value: str) -> int:
    text = _normalize_space(value)
    folded = text.casefold()
    if not text:
        return -1000
    score = 0
    if 20 <= len(text) <= 220:
        score += 5
    elif 10 <= len(text) <= 300:
        score += 1
    else:
        score -= 5
    word_count = len(text.split())
    score += 4 if 4 <= word_count <= 30 else -2
    if re.search(r"[a-z]{3}", folded):
        score += 2
    if re.search(r"[\u3400-\u9fff]{4}", text):
        score += 2
    artifact_markers = (
        "contents list",
        "copyright",
        "journal of physics",
        "open access proceedings",
        "sample manuscript",
        "sciencedirect",
        ".indd",
        ".pdf",
        "http://",
        "https://",
    )
    score -= 8 * sum(marker in folded for marker in artifact_markers)
    if "©" in text or DOI_RE.search(text):
        score -= 8
    if re.fullmatch(r"[\w.-]+\.(?:indd|pdf)", text, re.IGNORECASE):
        score -= 12
    if folded in {"title", "标题", "untitled", "document"}:
        score -= 20
    return score


def _document_from_tei(
    xml_bytes: bytes,
    *,
    file_name: str,
    content_digest: str,
    parser_version: str,
) -> ScientificDocument:
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise DomainQueryError(
            "knowledge_grobid_response_invalid",
            "GROBID returned malformed TEI XML",
        ) from exc
    title = _xml_text(root.find(".//{*}titleStmt/{*}title")) or Path(file_name).stem
    author_elements = [
        *root.findall(".//{*}titleStmt/{*}author"),
        *root.findall(".//{*}sourceDesc//{*}analytic/{*}author"),
    ]
    authors = tuple(
        dict.fromkeys(
            text
            for text in (_xml_text(author) for author in author_elements)
            if text
        )
    )
    blocks: list[ScientificBlock] = []
    citation_mentions: list[
        tuple[ScientificBlock, str, tuple[str, ...], int | None, int | None]
    ] = []
    ordinal = 0

    def append_block(
        kind: str,
        text: str,
        section: tuple[str, ...],
        element: ElementTree.Element,
    ) -> ScientificBlock | None:
        nonlocal ordinal
        normalized = _normalize_space(text)
        if not normalized:
            return None
        ordinal += 1
        page, coords = _tei_page_and_coords(element)
        block = _make_block(
            ordinal,
            kind,
            normalized,
            page,
            section,
            metadata={"coords": coords} if coords else {},
        )
        blocks.append(block)
        for raw_text, targets in _tei_bibr_mentions(element):
            start = normalized.find(raw_text)
            citation_mentions.append(
                (
                    block,
                    raw_text,
                    targets,
                    start if start >= 0 else None,
                    start + len(raw_text) if start >= 0 else None,
                )
            )
        return block

    def walk(container: ElementTree.Element, section: tuple[str, ...]) -> None:
        for child in list(container):
            name = _local_name(child.tag)
            if name == "div":
                head = child.find("./{*}head")
                heading = _xml_text(head)
                next_section = (*section, heading) if heading else section
                if heading and head is not None:
                    append_block("heading", heading, next_section, head)
                walk(child, next_section)
            elif name == "p":
                append_block("paragraph", _xml_text(child), section, child)
            elif name == "figure":
                figure_type = str(child.attrib.get("type") or "").casefold()
                kind = "table_caption" if figure_type == "table" else "figure_caption"
                caption = (
                    _xml_text(child.find("./{*}head"))
                    or _xml_text(child.find("./{*}figDesc"))
                    or _xml_text(child)
                )
                append_block(kind, caption, section, child)
            elif name == "formula":
                append_block("formula", _xml_text(child), section, child)

    body = root.find(".//{*}text/{*}body")
    if body is not None:
        walk(body, ())
    references: list[ScientificReference] = []
    tei_reference_ids: dict[str, str] = {}
    for entry in root.findall(".//{*}listBibl/{*}biblStruct"):
        raw_text = _xml_text(entry)
        if not raw_text:
            continue
        doi = ""
        for identifier in entry.findall(".//{*}idno"):
            if str(identifier.attrib.get("type") or "").casefold() == "doi":
                doi = _xml_text(identifier)
                break
        page, _coords = _tei_page_and_coords(entry)
        reference_id = f"ref-{len(references) + 1:04d}"
        references.append(
            ScientificReference(
                reference_id=reference_id,
                raw_text=raw_text,
                page=page,
                doi=doi or _first_doi(raw_text),
                title=_xml_text(entry.find(".//{*}title")),
            )
        )
        xml_id = str(
            entry.attrib.get("{http://www.w3.org/XML/1998/namespace}id")
            or entry.attrib.get("id")
            or ""
        ).strip()
        if xml_id:
            tei_reference_ids[xml_id] = reference_id
    citations: list[ScientificCitation] = []
    citation_warnings: list[str] = []
    for block, raw_text, targets, char_start, char_end in citation_mentions:
        unresolved = [
            target for target in targets if target not in tei_reference_ids
        ]
        citation_warnings.extend(
            f"unresolved_citation_target:{target}"
            for target in unresolved
            if f"unresolved_citation_target:{target}" not in citation_warnings
        )
        resolved = tuple(
            dict.fromkeys(
                tei_reference_ids[target]
                for target in targets
                if target in tei_reference_ids
            )
        )
        if not resolved:
            continue
        digest = sha256(
            (
                f"{block.block_id}\0{char_start}\0{','.join(resolved)}"
            ).encode("utf-8")
        ).hexdigest()[:10]
        citations.append(
            ScientificCitation(
                citation_id=f"cite-{len(citations) + 1:05d}-{digest}",
                block_id=block.block_id,
                reference_ids=resolved,
                raw_text=raw_text,
                page=block.page_start,
                char_start=char_start,
                char_end=char_end,
            )
        )
    if not blocks:
        raise DomainQueryError(
            "knowledge_grobid_response_invalid",
            "GROBID TEI contained no indexable body blocks",
        )
    page_values = [
        block.page_end
        for block in blocks
        if block.page_end is not None
    ]
    return ScientificDocument(
        document_id=f"pdf-{content_digest[:24]}",
        title=title[:300],
        authors=authors,
        content_hash=f"sha256:{content_digest}",
        page_count=max(page_values, default=0),
        parser_name="grobid",
        parser_version=parser_version,
        blocks=tuple(blocks),
        references=tuple(references),
        citations=tuple(citations),
        metadata={"file_name": file_name, "tei": True},
        warnings=(
            *(
                ("grobid_page_count_unavailable",)
                if not page_values
                else ()
            ),
            *citation_warnings,
        ),
    )


def _tei_bibr_mentions(
    element: ElementTree.Element,
) -> list[tuple[str, tuple[str, ...]]]:
    mentions: list[tuple[str, tuple[str, ...]]] = []
    for reference in element.findall(".//{*}ref"):
        if str(reference.attrib.get("type") or "").casefold() != "bibr":
            continue
        raw_text = _xml_text(reference)
        targets = tuple(
            item.removeprefix("#")
            for item in re.split(
                r"\s+",
                str(reference.attrib.get("target") or "").strip(),
            )
            if item
        )
        if raw_text and targets:
            mentions.append((raw_text, targets))
    return mentions


def _multipart_pdf_body(boundary: str, file_name: str, raw: bytes) -> bytes:
    fields = [
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="input"; '
            f'filename="{_safe_header_filename(file_name)}"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode("ascii"),
        raw,
        (
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="includeRawCitations"\r\n\r\n'
            "1\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="teiCoordinates"\r\n\r\n'
            "figure\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="teiCoordinates"\r\n\r\n'
            "biblStruct\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="teiCoordinates"\r\n\r\n'
            "ref\r\n"
            f"--{boundary}--\r\n"
        ).encode("ascii"),
    ]
    return b"".join(fields)


def _validate_grobid_url(value: str, *, allow_remote: bool) -> str:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DomainQueryError(
            "knowledge_pdf_parser_configuration_error",
            "GROBID base URL must be an absolute HTTP(S) URL",
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DomainQueryError(
            "knowledge_pdf_parser_configuration_error",
            "GROBID base URL must not contain credentials, query, or fragment",
        )
    loopback = parsed.hostname.casefold() in {"127.0.0.1", "::1", "localhost"}
    if not loopback and not allow_remote:
        raise DomainQueryError(
            "knowledge_grobid_remote_blocked",
            "remote GROBID requires SCHOLARFLOW_GROBID_ALLOW_REMOTE=true",
        )
    if not loopback and parsed.scheme != "https":
        raise DomainQueryError(
            "knowledge_grobid_remote_blocked",
            "remote GROBID must use HTTPS",
        )
    return str(value).rstrip("/")


def _tei_page_and_coords(
    element: ElementTree.Element,
) -> tuple[int | None, str]:
    coords = str(element.attrib.get("coords") or "").strip()
    if not coords:
        return None, ""
    first = coords.split(";", 1)[0].split(",", 1)[0].strip()
    try:
        return int(first), coords[:500]
    except ValueError:
        return None, coords[:500]


def _xml_text(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    return _normalize_space(" ".join(element.itertext()))


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _margin_key(value: str) -> str:
    normalized = _normalize_space(value).casefold()
    return re.sub(r"\d+", "<n>", normalized)


def _first_doi(text: str) -> str:
    match = DOI_RE.search(str(text or ""))
    return match.group(0).rstrip(".,;)") if match else ""


def _render_block_markdown(block: ScientificBlock) -> str:
    if block.kind == "heading":
        return f"## {block.text}"
    if block.kind == "reference_entry":
        return f"- {block.text}"
    if block.kind in {"figure_caption", "table_caption", "formula"}:
        return f"> [{block.kind}] {block.text}"
    return block.text


def _safe_header_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(value).name)[:180] or "paper.pdf"


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def _truthy(value: str) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}
