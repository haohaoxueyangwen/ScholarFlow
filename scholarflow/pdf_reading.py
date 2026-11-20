"""Scoped reads of already-staged PDF evidence. No parsing, model or writes.

Host authorization binds an exact import and immutable source/parse versions.
This is an engineering source boundary, not yet a native Pi capability.
"""
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re

from .domain_queries import DomainQueryError
from .mutations import record_fingerprint
from .reader_workspace import MAX_READER_PDF_BYTES
from .scientific_document import ScientificDocument
from .storage import ScholarFlowStore

MAX_STRUCTURE_BYTES = 32_000_000
MAX_FRAGMENT_BYTES = 64_000


def _fail(code, message):
    raise DomainQueryError(code, message)


def _hash(raw):
    return "sha256:" + sha256(raw).hexdigest()


def _id(value, pattern):
    return isinstance(value, str) and len(value) <= 200 and re.fullmatch(pattern, value) is not None


@dataclass(frozen=True)
class PdfReadGrant:
    principal: str
    documents: dict

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {"principal", "documents"}:
            _fail("permission_denied", "Exact host PDF grant required")
        principal, documents = value["principal"], value["documents"]
        if not isinstance(principal, str) or not principal or principal != principal.strip() or len(principal) > 160:
            _fail("permission_denied", "Host principal required")
        if not isinstance(documents, dict) or not 1 <= len(documents) <= 100:
            _fail("permission_denied", "Bounded exact document grants required")
        for alias, item in documents.items():
            if not _id(alias, r"[A-Za-z0-9_-]+") or not isinstance(item, dict) or set(item) != {"stage_id", "import_id", "content_hash", "structure_hash", "page_ranges"}:
                _fail("permission_denied", "Invalid exact document binding")
            if not _id(item["stage_id"], r"stage-.+") or not _id(item["import_id"], r"pdf_[a-f0-9]{24}"):
                _fail("permission_denied", "Canonical project and import identities required")
            if any(not _id(item[key], r"sha256:[a-f0-9]{64}") for key in ("content_hash", "structure_hash")):
                _fail("permission_denied", "Source and structure hashes must be pinned")
            ranges = item["page_ranges"]
            if not isinstance(ranges, list) or not 1 <= len(ranges) <= 100:
                _fail("permission_denied", "Explicit page ranges required")
            last = 0
            for pair in ranges:
                if not isinstance(pair, list) or len(pair) != 2 or any(type(x) is not int for x in pair) or not 1 <= pair[0] <= pair[1] <= 100000 or pair[0] <= last:
                    _fail("permission_denied", "Sorted disjoint positive page ranges required")
                last = pair[1]
        return cls(principal, deepcopy(documents))


class PdfSourceService:
    def __init__(self, data_dir, *, grant_guard=None):
        self.root = Path(data_dir).absolute()
        self.grant_guard = grant_guard or (lambda: None)

    def _safe(self, path):
        if not self.root.is_dir() or self.root.resolve() != self.root:
            _fail("permission_denied", "Existing canonical data root required")
        for component in (path, *path.parents):
            if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
                _fail("permission_denied", "Aliased source paths are not allowed")
        if self.root not in path.parents or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
            _fail("permission_denied", "Regular scoped source file required")
        return path

    def _bytes(self, path, maximum):
        self._safe(path)
        try:
            if path.stat().st_size > maximum:
                _fail("resource_limit", "Source exceeds this operation's byte budget")
            raw = path.read_bytes()
        except FileNotFoundError:
            _fail("source_unavailable", "Approved staged source is missing")
        if len(raw) > maximum:
            _fail("resource_limit", "Source exceeded its byte budget during read")
        return raw

    def _load(self, stage_id, document_id, grant):
        self.grant_guard()
        if not _id(stage_id, r"stage-.+") or not _id(document_id, r"[A-Za-z0-9_-]+") or not isinstance(grant, PdfReadGrant) or document_id not in grant.documents or grant.documents[document_id]["stage_id"] != stage_id:
            _fail("permission_denied", "Document is not authorized for this project")
        target = grant.documents[document_id]
        directory = self.root / "_reader/imports" / target["import_id"]
        paths = [self.root / "project_stages.json", directory / "metadata.json", directory / "document.pdf", directory / "structured.json"]
        raw = [self._bytes(paths[0], MAX_STRUCTURE_BYTES), self._bytes(paths[1], 1_000_000),
               self._bytes(paths[2], MAX_READER_PDF_BYTES), self._bytes(paths[3], MAX_STRUCTURE_BYTES)]
        try:
            metadata, payload = json.loads(raw[1]), json.loads(raw[3])
        except (UnicodeError, ValueError, TypeError):
            _fail("source_unavailable", "Approved parsed source is unreadable")
        if not isinstance(metadata, dict) or not isinstance(payload, dict):
            _fail("source_unavailable", "Source cache envelope is invalid")
        if _hash(raw[2]) != target["content_hash"] or _hash(raw[3]) != target["structure_hash"]:
            _fail("source_version_conflict", "PDF or approved parse changed; host re-review required")
        if metadata.get("import_id") != target["import_id"] or metadata.get("content_hash") != target["content_hash"] or payload.get("content_hash") != target["content_hash"]:
            _fail("source_version_conflict", "Source identity disagrees with approved content")
        if metadata.get("status") != "parsed" or stage_id not in (metadata.get("project_stage_ids") or []):
            _fail("source_unavailable", "Source is not currently parsed and associated with the authorized project")
        if sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest() != metadata.get("structured_digest"):
            _fail("source_version_conflict", "Parsed cache no longer matches its committed metadata")
        if type(payload.get("page_count")) is not int or not isinstance(payload.get("blocks"), list) or any(
                not isinstance(b, dict) or not isinstance(b.get("text"), str) or not isinstance(b.get("block_id"), str)
                or not isinstance(b.get("section_path", []), list) or any(not isinstance(s, str) for s in b.get("section_path", []))
                for b in payload["blocks"]):
            _fail("source_unavailable", "Parsed source types are invalid; no coercion into evidence")
        document = ScientificDocument.from_dict(payload)
        if type(document.page_count) is not int or document.page_count < 1 or any(pair[1] > document.page_count for pair in target["page_ranges"]):
            _fail("invalid_argument", "Granted pages exceed the parsed document")
        stage = ScholarFlowStore(self.root).get_project_stage(stage_id)
        if stage is None:
            _fail("source_unavailable", "Authorized project no longer exists")
        blocks, seen = [], set()
        for block in document.blocks:
            if not block.block_id or block.block_id in seen or not isinstance(block.text, str):
                _fail("source_unavailable", "Ambiguous or invalid parsed block identity")
            seen.add(block.block_id)
            a, b = block.page_start, block.page_end
            if a is None or b is None:
                continue  # Unanchored text is never delivered as approved page evidence.
            if type(a) is not int or type(b) is not int or not 1 <= a <= b <= document.page_count:
                _fail("source_unavailable", "Invalid parsed page anchor")
            if any(first <= a <= b <= last for first, last in target["page_ranges"]):
                blocks.append(block)
        self.grant_guard()
        if any(self._bytes(path, MAX_READER_PDF_BYTES if i == 2 else MAX_STRUCTURE_BYTES) != before for i, (path, before) in enumerate(zip(paths, raw))):
            _fail("source_version_conflict", "Source changed during snapshot read")
        self.grant_guard()
        versions = {"source_type": "pdf_fragment", "stage_id": stage_id, "stage_fingerprint": record_fingerprint(stage.to_dict()),
                    "document_id": document_id, "import_id": target["import_id"], "scientific_document_id": document.document_id,
                    "content_hash": target["content_hash"], "structure_hash": target["structure_hash"],
                    "parser": {"name": document.parser_name, "version": document.parser_version}, "schema_version": document.schema_version}
        return document, blocks, versions

    def inspect(self, stage_id, document_id, grant, *, offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            _fail("invalid_argument", "Invalid outline pagination")
        document, blocks, versions = self._load(stage_id, document_id, grant)
        items = [{"block_id": b.block_id, "kind": b.kind, "page_start": b.page_start, "page_end": b.page_end,
                  "character_count": len(b.text), "utf8_bytes": len(b.text.encode("utf-8"))} for b in blocks[offset:offset + limit]]
        return {"source": versions, "title": document.title, "page_count": document.page_count,
                "allowed_page_ranges": deepcopy(grant.documents[document_id]["page_ranges"]), "items": items,
                "total_allowed_blocks": len(blocks), "next_offset": offset + len(items) if offset + len(items) < len(blocks) else None,
                "readable": any(b.text.strip() for b in blocks), "warnings": list(document.warnings),
                "evidence_scope": "approved page-anchored blocks only; layout and OCR quality require review"}

    def fragment(self, stage_id, document_id, grant, *, block_id, char_start=0, char_end=None):
        _, blocks, versions = self._load(stage_id, document_id, grant)
        block = next((b for b in blocks if b.block_id == block_id), None)
        if block is None:
            _fail("permission_denied", "Exact block is not within authorized page evidence")
        end = len(block.text) if char_end is None else char_end
        if type(char_start) is not int or type(end) is not int or not 0 <= char_start < end <= len(block.text):
            _fail("invalid_argument", "Exact source character range required")
        text = block.text[char_start:end]
        if not text.strip():
            _fail("source_unavailable", "Selected fragment has no readable text")
        if len(text.encode("utf-8")) > MAX_FRAGMENT_BYTES:
            _fail("resource_limit", "Read a smaller explicit character range; no silent truncation")
        anchor = {"block_id": block.block_id, "page_start": block.page_start, "page_end": block.page_end,
                  "char_start": char_start, "char_end": end, "text_hash": _hash(text.encode("utf-8"))}
        allowed_headings = {b.text for b in blocks if b.kind == "heading"}
        verified_sections = [s for s in block.section_path if s in allowed_headings]
        return {"source": versions, "anchor": anchor, "text": text, "kind": block.kind,
                "section_path": verified_sections, "section_path_complete": len(verified_sections) == len(block.section_path),
                "complete_block": char_start == 0 and end == len(block.text),
                "source_role": "parsed original evidence, not model translation or note", "writes": False}
