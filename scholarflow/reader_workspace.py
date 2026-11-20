"""Local PDF staging and user-authored reader annotations.

Reader imports are deliberately not domain Papers or AI knowledge sources until
the existing proposal and knowledge-sync boundaries are crossed explicitly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
import math
import os
from pathlib import Path
import re
import secrets
from typing import Any, Mapping

from .domain_queries import DomainQueryError
from .knowledge_ingestion import IngestionCandidate, discover_file_sources
from .persistence import atomic_write_json, file_lock
from .scientific_document import ScientificDocument, parse_scientific_pdf


MAX_READER_PDF_BYTES = 80_000_000
_IMPORT_ID = re.compile(r"^pdf_[0-9a-f]{24}$")
_ANNOTATION_COLORS = {"yellow", "mint", "blue", "rose"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReaderWorkspaceStore:
    """Own staged PDFs and reading notes without promoting either to truth."""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "_reader"
        self.import_root = self.root / "imports"
        self.annotations_path = self.root / "annotations.json"

    def import_pdf(
        self,
        filename: str,
        payload: bytes,
        *,
        origin: Mapping[str, Any] | None = None,
        bibliography: Mapping[str, Any] | None = None,
        project_stage_ids: list[str] | None = None,
        force_reparse: bool = False,
    ) -> dict[str, Any]:
        if not payload or len(payload) > MAX_READER_PDF_BYTES:
            raise DomainQueryError(
                "reader_pdf_size_invalid",
                "PDF must be between 1 byte and 80 MB",
            )
        if b"%PDF-" not in payload[:1024]:
            raise DomainQueryError(
                "reader_pdf_invalid",
                "selected file does not have a valid PDF header",
            )
        digest = sha256(payload).hexdigest()
        import_id = f"pdf_{digest[:24]}"
        directory = self.import_root / import_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "document.pdf"
        metadata_path = directory / "metadata.json"
        structured_path = directory / "structured.json"
        if not target.exists():
            temporary = directory / f".{secrets.token_hex(8)}.tmp"
            temporary.write_bytes(payload)
            os.replace(temporary, target)
        elif sha256(target.read_bytes()).hexdigest() != digest:
            raise DomainQueryError("reader_store_invalid", "stored PDF no longer matches its content identity")

        with file_lock(metadata_path, timeout_seconds=30.0):
            existing = self._read_json(metadata_path, default={})
            previous = self._read_json(structured_path, default=None)
            signature = self._parser_signature()
            cached = (not force_reparse and existing.get("status") == "parsed"
                      and existing.get("parser_signature") == signature and isinstance(previous, dict)
                      and existing.get("structured_digest") == sha256(json.dumps(previous, sort_keys=True).encode()).hexdigest()
                      and previous.get("content_hash") == f"sha256:{digest}")
            structured = previous if cached else None
            parse_error = ""
            if not cached:
                try:
                    document = parse_scientific_pdf(target)
                    structured = document.to_dict()
                    structured["content_hash"] = f"sha256:{digest}"
                    atomic_write_json(structured_path, structured)
                except DomainQueryError as exc:
                    parse_error = str(exc.code)
            safe_filename = self._safe_filename(filename)
            incoming_origin = self._safe_origin(origin)
            incoming_bibliography = self._safe_bibliography(bibliography)
            origins = list(existing.get("origins") or ([existing["origin"]] if existing.get("origin") else []))
            if incoming_origin and incoming_origin not in origins:
                origins.append(incoming_origin)
            projects = sorted(set(existing.get("project_stage_ids") or []) | set(project_stage_ids or []))
            metadata = {
                **existing, "schema_version": 1, "import_id": import_id,
                "document_ref": f"local_pdf:{import_id}", "filename": safe_filename,
                "size_bytes": len(payload), "content_hash": f"sha256:{digest}",
                "created_at": str(existing.get("created_at") or _now()), "updated_at": _now(),
                "status": "parsed" if structured is not None else "parsed_stale" if previous else "staged",
                "structured_digest": sha256(json.dumps(structured or previous, sort_keys=True).encode()).hexdigest() if structured or previous else "",
                "parse_error": parse_error, "parser_signature": signature,
                "previous_parse_available": bool(previous and structured is None),
                "title": str((structured or previous or {}).get("title") or incoming_bibliography.get("title") or (existing.get("bibliography") or {}).get("title") or Path(safe_filename).stem),
                "authors": list((structured or previous or {}).get("authors") or (existing.get("bibliography") or {}).get("authors") or incoming_bibliography.get("authors") or []),
                "page_count": int((structured or previous or {}).get("page_count") or 0),
                "reference_count": len((structured or previous or {}).get("references") or []),
                "origin": {**incoming_origin, **(existing.get("origin") or {})},
                "origins": origins, "project_stage_ids": projects,
                "bibliography": {**incoming_bibliography, **(existing.get("bibliography") or {})},
            }
            atomic_write_json(metadata_path, metadata)
        return self._public_import(metadata)

    @staticmethod
    def _parser_signature() -> str:
        settings = {key: value for key, value in os.environ.items() if key.startswith(("SCHOLARFLOW_PDF_", "SCHOLARFLOW_GROBID_", "SCHOLARFLOW_OCR_", "SCHOLARFLOW_TESSERACT_"))}
        try:
            settings["pypdf_version"] = version("pypdf")
        except PackageNotFoundError:
            settings["pypdf_version"] = "unavailable"
        return sha256(json.dumps({"cache_version": 1, "settings": settings}, sort_keys=True).encode()).hexdigest()

    def set_project_links(self, import_id: str, project_stage_ids: list[str]) -> dict[str, Any]:
        path = self._import_directory(import_id) / "metadata.json"
        with file_lock(path):
            metadata = self._read_json(path, default=None)
            if metadata is None:
                raise DomainQueryError("not_found", "reader PDF import was not found")
            metadata["project_stage_ids"] = sorted(set(project_stage_ids))
            metadata["updated_at"] = _now()
            atomic_write_json(path, metadata)
        return self._public_import(metadata)

    def document_links(self, import_id: str) -> dict[str, Any]:
        from .storage import ScholarFlowStore

        imported = self.get_import(import_id)
        store = ScholarFlowStore(self.root.parent)
        origins = [imported.get("origin") or {}, *(imported.get("origins") or [])]
        origin_ids = {str(item.get("paper_id") or "") for item in origins}
        papers = [paper.paper_id for paper in store.list_papers() if paper.external_ids.get("local_pdf") == import_id or paper.paper_id in origin_ids]
        projects = set(imported.get("project_stage_ids") or [])
        refs = {f"paper:{paper_id}" for paper_id in papers}
        projects.update(project.stage_id for project in store.list_project_stages() if refs.intersection(project.evidence_refs))
        return {"document_ref": f"local_pdf:{import_id}", "paper_ids": sorted(papers), "project_stage_ids": sorted(projects)}

    def _annotation_identity(self, target_ref: str) -> tuple[str, set[str]]:
        from .storage import ScholarFlowStore

        target = self._target_ref(target_ref)
        import_id = target.removeprefix("local_pdf:") if target.startswith("local_pdf:") else ""
        if target.startswith("paper:"):
            paper = ScholarFlowStore(self.root.parent).get_paper(target.removeprefix("paper:"))
            import_id = str(paper.external_ids.get("local_pdf") or "") if paper else ""
        aliases = {target}
        if import_id and _IMPORT_ID.fullmatch(import_id):
            canonical = f"local_pdf:{import_id}"
            aliases.add(canonical)
            try:
                links = self.document_links(import_id)
                aliases.update(f"paper:{paper_id}" for paper_id in links["paper_ids"])
            except DomainQueryError as exc:
                if exc.code != "not_found":
                    raise
            return canonical, aliases
        return target, aliases

    def list_imports(self, *, limit: int = 100) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        if self.import_root.is_dir():
            for metadata_path in sorted(
                self.import_root.glob("pdf_*/metadata.json"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            ):
                metadata = self._read_json(metadata_path, default={})
                if metadata:
                    items.append(self._public_import(metadata))
                if len(items) >= max(1, min(int(limit), 500)):
                    break
        return {"count": len(items), "items": items, "staged_only": True}

    def get_import(self, import_id: str) -> dict[str, Any]:
        directory = self._import_directory(import_id)
        metadata = self._read_json(directory / "metadata.json", default=None)
        if not isinstance(metadata, dict):
            raise DomainQueryError("not_found", "reader PDF import was not found")
        return self._public_import(metadata)

    def pdf_path(self, import_id: str) -> Path:
        target = self._import_directory(import_id) / "document.pdf"
        if not target.is_file() or target.stat().st_size <= 0:
            raise DomainQueryError("not_found", "reader PDF file was not found")
        return target

    def structured_document(self, import_id: str) -> dict[str, Any] | None:
        imported = self.get_import(import_id)
        if imported.get("status") != "parsed":
            return None
        path = self._import_directory(import_id) / "structured.json"
        payload = self._read_json(path, default=None)
        metadata = self._read_json(path.parent / "metadata.json", default={})
        if metadata.get("structured_digest") and sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest() != metadata["structured_digest"]:
            return None
        return payload if isinstance(payload, dict) and payload.get("content_hash") == imported.get("content_hash") else None

    def ingestion_candidate(self, import_id: str) -> IngestionCandidate:
        directory = self._import_directory(import_id)
        cached = self.structured_document(import_id)
        if cached is not None:
            scientific = ScientificDocument.from_dict(cached)
            text = scientific.to_index_markdown()
            raw_digest = scientific.content_hash.removeprefix("sha256:")
            digest = sha256(f"{raw_digest}\0{scientific.schema_version}\0{scientific.parser_name}\0{scientific.parser_version}\0{text}".encode()).hexdigest()
            links = self.document_links(import_id)
            return IngestionCandidate(
                source_ref=f"local-pdf:{import_id}:document.pdf", source_type="local-pdf",
                title=scientific.title, text=text, scope=f"local-pdf:{import_id}",
                locator=str(directory / "document.pdf"), content_digest=digest,
                source_version=f"pdf:{raw_digest}:schema-{scientific.schema_version}:{scientific.parser_name}:{scientific.parser_version}",
                metadata={"root_id": import_id, "relative_path": "document.pdf", "extension": ".pdf",
                          "source_content_hash": scientific.content_hash, "scientific_document": scientific.structure_summary,
                          **links, "paper_id": next(iter(links["paper_ids"]), "")},
                structured_document=cached,
            )
        if self.get_import(import_id).get("parse_error") or self.get_import(import_id).get("structured_digest"):
            raise DomainQueryError("knowledge_source_unavailable", "PDF parsing failed; retry parsing before indexing")
        candidates = discover_file_sources(
            directory,
            source_type="local-pdf",
            root_id=import_id,
            extensions={".pdf"},
            max_file_bytes=MAX_READER_PDF_BYTES,
        )
        if len(candidates) != 1:
            raise DomainQueryError(
                "knowledge_source_unavailable",
                "reader import does not contain exactly one parseable PDF",
            )
        links = self.document_links(import_id)
        return replace(candidates[0], metadata={**candidates[0].metadata, **links, "paper_id": next(iter(links["paper_ids"]), "")})

    def list_annotations(self, target_ref: str) -> dict[str, Any]:
        target, aliases = self._annotation_identity(target_ref)
        state = self._annotation_state()
        items = [
            dict(item)
            for item in state["annotations"].values()
            if isinstance(item, dict) and item.get("target_ref") in aliases
        ]
        items.sort(key=lambda item: (int(item.get("page") or 0), str(item.get("created_at") or "")))
        return {"target_ref": target, "count": len(items), "items": items, "user_authored": True}

    def create_annotation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        target_ref, _ = self._annotation_identity(str(payload.get("target_ref") or ""))
        quote = self._bounded_text(payload.get("quote"), "quote", 5000, required=False)
        note = self._bounded_text(payload.get("note"), "note", 5000, required=False)
        if not quote and not note:
            raise DomainQueryError("invalid_argument", "annotation requires a quote or note")
        page = payload.get("page")
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 100_000:
            raise DomainQueryError("invalid_argument", "annotation page must be a positive integer")
        color = str(payload.get("color") or "yellow").strip().casefold()
        if color not in _ANNOTATION_COLORS:
            raise DomainQueryError("invalid_argument", "annotation color is unsupported")
        anchor = self._annotation_anchor(payload.get("anchor"), page)
        annotation_id = f"ann_{secrets.token_hex(12)}"
        now = _now()
        annotation = {
            "annotation_id": annotation_id,
            "target_ref": target_ref,
            "page": page,
            "quote": quote,
            "note": note,
            "color": color,
            "anchor": anchor,
            "created_at": now,
            "updated_at": now,
            "user_authored": True,
            "authoritative": False,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with file_lock(self.annotations_path):
            state = self._annotation_state()
            state["annotations"][annotation_id] = annotation
            atomic_write_json(self.annotations_path, state)
        return dict(annotation)

    def delete_annotation(self, annotation_id: str) -> dict[str, Any]:
        normalized = str(annotation_id or "").strip()
        if not re.fullmatch(r"ann_[0-9a-f]{24}", normalized):
            raise DomainQueryError("invalid_argument", "annotation id is invalid")
        self.root.mkdir(parents=True, exist_ok=True)
        with file_lock(self.annotations_path):
            state = self._annotation_state()
            removed = state["annotations"].pop(normalized, None)
            if removed is None:
                raise DomainQueryError("not_found", "annotation was not found")
            atomic_write_json(self.annotations_path, state)
        return {"annotation_id": normalized, "status": "deleted"}

    def _annotation_state(self) -> dict[str, Any]:
        payload = self._read_json(
            self.annotations_path,
            default={"schema_version": 1, "annotations": {}},
        )
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("annotations"), dict)
        ):
            raise DomainQueryError("reader_annotations_invalid", "reader annotation store is invalid")
        return payload

    @staticmethod
    def _annotation_anchor(value: Any, page: int) -> dict[str, Any] | None:
        if value is None:
            return None
        def invalid():
            raise DomainQueryError("invalid_argument", "PDF annotation anchor is invalid or outside the selected page")
        if not isinstance(value, dict) or value.get("kind") != "pdf_text_range_v1" or value.get("page") != page or not re.fullmatch(r"[0-9a-fA-F]{16,128}", str(value.get("document_fingerprint") or "")):
            invalid()
        points = {}
        for name in ("start", "end"):
            point = value.get(name)
            if not isinstance(point, dict) or any(isinstance(point.get(key), bool) or not isinstance(point.get(key), int) or not 0 <= point[key] <= 1_000_000 for key in ("item_index", "offset")):
                invalid()
            points[name] = {key: point[key] for key in ("item_index", "offset")}
        if (points["end"]["item_index"], points["end"]["offset"]) < (points["start"]["item_index"], points["start"]["offset"]):
            invalid()
        rects = value.get("rects")
        if not isinstance(rects, list) or not 1 <= len(rects) <= 300:
            invalid()
        clean = []
        for rect in rects:
            if not isinstance(rect, dict) or any(isinstance(rect.get(key), bool) or not isinstance(rect.get(key), (float, int)) or not math.isfinite(rect[key]) or not 0 <= rect[key] <= 1 for key in ("x", "y", "width", "height")):
                invalid()
            if rect["width"] <= 0 or rect["height"] <= 0 or rect["x"] + rect["width"] > 1.002 or rect["y"] + rect["height"] > 1.002:
                invalid()
            clean.append({key: rect[key] for key in ("x", "y", "width", "height")})
        return {"kind": "pdf_text_range_v1", "document_fingerprint": value["document_fingerprint"], "page": page, **points, "rects": clean}

    def _import_directory(self, import_id: str) -> Path:
        normalized = str(import_id or "").strip()
        if not _IMPORT_ID.fullmatch(normalized):
            raise DomainQueryError("invalid_argument", "reader import id is invalid")
        return self.import_root / normalized

    @staticmethod
    def _read_json(path: Path, *, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DomainQueryError("reader_store_invalid", "reader workspace record is unreadable") from exc

    @staticmethod
    def _safe_filename(value: str) -> str:
        normalized = str(value or "document.pdf").replace("\\", "/").rsplit("/", 1)[-1]
        normalized = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", normalized).strip(" .")
        if not normalized.casefold().endswith(".pdf"):
            normalized = f"{normalized or 'document'}.pdf"
        return normalized[:180]

    @staticmethod
    def _safe_origin(origin: Mapping[str, Any] | None) -> dict[str, str]:
        if not isinstance(origin, Mapping):
            return {}
        allowed = {}
        for key in ("paper_id", "source", "zotero_key", "doi", "source_host", "source_url_sha256"):
            value = str(origin.get(key) or "").strip()
            if value:
                allowed[key] = value[:300]
        return allowed

    @staticmethod
    def _safe_bibliography(value: Mapping[str, Any] | None) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        title = " ".join(str(value.get("title") or "").split())[:500]
        doi = " ".join(str(value.get("doi") or "").split())[:300]
        authors = value.get("authors")
        normalized_authors = [" ".join(str(item).split())[:160] for item in authors[:100] if str(item).strip()] if isinstance(authors, list) else []
        try:
            year = int(value.get("year")) if not isinstance(value.get("year"), bool) else None
        except (TypeError, ValueError, OverflowError):
            year = None
        if year is not None and not 1000 <= year <= 2100:
            year = None
        return {key: item for key, item in {"title": title, "doi": doi, "authors": normalized_authors, "year": year}.items() if item not in (None, "", [])}

    @staticmethod
    def _public_import(metadata: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: metadata.get(key)
            for key in (
                "schema_version",
                "import_id",
                "filename",
                "size_bytes",
                "content_hash",
                "created_at",
                "updated_at",
                "status",
                "parse_error",
                "title",
                "authors",
                "page_count",
                "reference_count",
                "origin",
                "origins",
                "bibliography",
                "document_ref",
                "project_stage_ids",
                "previous_parse_available",
            )
        }

    @staticmethod
    def _target_ref(value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > 400 or not re.fullmatch(r"[A-Za-z0-9:._-]+", normalized):
            raise DomainQueryError("invalid_argument", "annotation target ref is invalid")
        if not normalized.startswith(("paper:", "local_pdf:")):
            raise DomainQueryError("invalid_argument", "annotation target must be a paper or local PDF")
        return normalized

    @staticmethod
    def _bounded_text(value: Any, field: str, limit: int, *, required: bool) -> str:
        normalized = " ".join(str(value or "").replace("\x00", " ").split())
        if required and not normalized:
            raise DomainQueryError("invalid_argument", f"{field} is required")
        if len(normalized) > limit:
            raise DomainQueryError("invalid_argument", f"{field} exceeds the {limit} character limit")
        return normalized
