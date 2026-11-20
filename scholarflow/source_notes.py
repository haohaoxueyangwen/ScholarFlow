"""Versioned human notes over the existing ResearchArtifact store, not AI RAG.

Version 1 stores paper summaries; version 2 stores verified PDF fragments.
Source locks cover cooperating store
writers; they are not an OS sandbox or proof of scientific source fidelity.
"""
from contextlib import contextmanager
import re

from .mutations import MutationError, record_fingerprint
from .persistence import file_lock
from .research_mutations import ResearchMutationService

MAX_SOURCES = 8
MAX_SUMMARY = 20000
MAX_SOURCE_TOTAL = 80000


def validate_source_snapshot(value):
    if isinstance(value, dict) and value.get("version") == 2:
        return validate_pdf_snapshot(value)
    def fingerprint(item):
        return isinstance(item, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", item)

    def canonical_id(item, prefix):
        return isinstance(item, str) and item == item.strip() and len(prefix) < len(item) <= 200 and item.startswith(prefix)

    if (not isinstance(value, dict) or set(value) != {"version", "stage_id", "stage_fingerprint", "sources"}
            or type(value["version"]) is not int or value["version"] != 1
            or not canonical_id(value["stage_id"], "stage-")
            or not fingerprint(value["stage_fingerprint"])):
        raise ValueError("invalid source snapshot header")
    sources = value["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        raise ValueError("invalid source snapshot sources")
    seen, total = set(), 0
    for source in sources:
        if (not isinstance(source, dict) or set(source) != {"paper_id", "record_fingerprint", "title", "summary", "source_type"}
                or not canonical_id(source["paper_id"], "paper-")
                or source["paper_id"] in seen or not fingerprint(source["record_fingerprint"])
                or source["source_type"] != "stored_summary"
                or not isinstance(source["title"], str) or not source["title"].strip()
                or not isinstance(source["summary"], str) or not source["summary"].strip()
                or len(source["summary"]) > MAX_SUMMARY):
            raise ValueError("invalid stored-summary snapshot")
        seen.add(source["paper_id"])
        total += len(source["summary"])
    if total > MAX_SOURCE_TOTAL:
        raise ValueError("source snapshot resource limit exceeded")


@contextmanager
def source_locks(store):
    # Application -> project -> paper -> artifact. Never acquire the common
    # application lock while already holding these source locks during apply.
    with file_lock(store.project_stages.file_path), file_lock(store.papers.file_path):
        yield


def read_sources(store, stage_id, paper_ids):
    stage = store.get_project_stage(stage_id)
    if stage is None:
        raise MutationError("not_found", "Source note project does not exist")
    sources, total = [], 0
    for paper_id in sorted(paper_ids):
        paper = store.get_paper(paper_id)
        if (paper is None or not isinstance(paper.title, str) or not paper.title.strip()
                or not isinstance(paper.summary, str) or not paper.summary.strip()):
            raise MutationError("source_unavailable", "Authorized paper has no stored summary; no full-text substitute is available")
        total += len(paper.summary)
        if len(paper.summary) > MAX_SUMMARY or total > MAX_SOURCE_TOTAL:
            raise MutationError("resource_limit", "Stored summaries exceed this operation's budget; no silent truncation")
        sources.append({"paper_id": paper_id, "record_fingerprint": record_fingerprint(paper.to_dict()),
            "title": paper.title, "summary": paper.summary, "source_type": "stored_summary"})
    snapshot = {"version": 1, "stage_id": stage_id, "stage_fingerprint": record_fingerprint(stage.to_dict()), "sources": sources}
    validate_source_snapshot(snapshot)
    return snapshot


def source_version_state(store, snapshot):
    validate_source_snapshot(snapshot)
    if snapshot["version"] == 2:
        return pdf_version_state(store, snapshot)
    stage = store.get_project_stage(snapshot["stage_id"])
    stage_matches = record_fingerprint(stage.to_dict() if stage else None) == snapshot["stage_fingerprint"]
    sources = []
    for source in snapshot["sources"]:
        current = store.get_paper(source["paper_id"])
        sources.append({"paper_id": source["paper_id"], "matches_current": bool(current and
            record_fingerprint(current.to_dict()) == source["record_fingerprint"])})
    return {"matches_current": stage_matches and all(item["matches_current"] for item in sources),
        "project_matches_current": stage_matches, "sources": sources,
        "observation": "current stored-record comparison; not edit history or PDF verification"}


class SourceNoteMutationService(ResearchMutationService):
    def propose_note(self, artifact_id, *, stage_id, mode, changes, source_versions, request_id=None):
        if (not isinstance(mode, str) or mode not in {"create", "update"}
                or not isinstance(changes, dict) or not changes or set(changes) - {"title", "content", "status"}
                or not isinstance(source_versions, dict) or not 1 <= len(source_versions) <= MAX_SOURCES):
            raise MutationError("invalid_argument", "Explicit note mode, fields and source versions required")
        with source_locks(self.store):
            snapshot = read_sources(self.store, stage_id, source_versions)
            if any(source_versions[item["paper_id"]] != item["record_fingerprint"] for item in snapshot["sources"]):
                raise MutationError("source_version_conflict", "Stored source changed; reread context before proposing")

            def normalize(values, creating):
                result = self._artifact_changes(values, creating)
                if creating:
                    result.update(kind="synthesis", project_stage_id=stage_id)
                result.update(source_snapshot=snapshot,
                    related_papers=[s["paper_id"] for s in snapshot["sources"]],
                    evidence_refs=["paper:" + s["paper_id"] for s in snapshot["sources"]])
                return result

            return self._propose("research_artifact.publish", "artifact_id", artifact_id, changes,
                getter=self.store.get_research_artifact, normalizer=normalize,
                existence="create_only" if mode == "create" else "update_only", request_id=request_id)


def snapshot_references(snapshot):
    validate_source_snapshot(snapshot)
    if snapshot["version"] == 1:
        papers = [s["paper_id"] for s in snapshot["sources"]]
        return papers, ["paper:" + p for p in papers]
    return [], ["pdf:" + s["source"]["import_id"] + "#" + s["anchor"]["block_id"] + ":" + str(s["anchor"]["char_start"]) + ":" + str(s["anchor"]["char_end"]) for s in snapshot["sources"]]


def validate_pdf_snapshot(value):
    if set(value) != {"version", "stage_id", "stage_fingerprint", "sources"} or type(value["version"]) is not int or value["version"] != 2 or not isinstance(value["stage_id"], str) or not value["stage_id"].startswith("stage-") or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value["stage_fingerprint"])):
        raise ValueError("Invalid PDF snapshot header")
    sources = value["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        raise ValueError("Bounded PDF sources required")
    seen, total = set(), 0
    for s in sources:
        if not isinstance(s, dict) or set(s) != {"source_id", "fingerprint", "source_type", "source", "anchor", "text"} or s["source_type"] != "pdf_fragment" or not isinstance(s["source_id"], str) or not re.fullmatch(r"pdf-source-[a-f0-9]{24}", s["source_id"]) or s["source_id"] in seen:
            raise ValueError("Invalid PDF source identity")
        source, anchor = s["source"], s["anchor"]
        if not isinstance(source, dict) or source.get("source_type") != "pdf_fragment" or source.get("stage_id") != value["stage_id"] or not isinstance(anchor, dict) or set(anchor) != {"block_id", "page_start", "page_end", "char_start", "char_end", "text_hash"}:
            raise ValueError("Invalid PDF source binding")
        if any(type(anchor[k]) is not int for k in ("page_start", "page_end", "char_start", "char_end")) or not 1 <= anchor["page_start"] <= anchor["page_end"] or not 0 <= anchor["char_start"] < anchor["char_end"] or not isinstance(s["text"], str) or len(s["text"]) != anchor["char_end"] - anchor["char_start"]:
            raise ValueError("Invalid PDF source range")
        import hashlib
        if anchor["text_hash"] != "sha256:" + hashlib.sha256(s["text"].encode("utf-8")).hexdigest() or s["fingerprint"] != record_fingerprint({"source":source,"anchor":anchor,"text":s["text"]}):
            raise ValueError("PDF source fingerprint mismatch")
        seen.add(s["source_id"]);total += len(s["text"])
    if total > MAX_SOURCE_TOTAL: raise ValueError("PDF snapshot resource limit")


def pdf_version_state(store, snapshot):
    from .pdf_reading import PdfReadGrant, PdfSourceService
    stage = store.get_project_stage(snapshot["stage_id"])
    stage_ok = record_fingerprint(stage.to_dict() if stage else None) == snapshot["stage_fingerprint"]
    states = []
    for item in snapshot["sources"]:
        source, anchor = item["source"], item["anchor"]
        try:
            grant = PdfReadGrant.from_dict({"principal":"source-freshness-observer", "documents":{source["document_id"]:{"stage_id":snapshot["stage_id"], "import_id":source["import_id"], "content_hash":source["content_hash"], "structure_hash":source["structure_hash"], "page_ranges":[[anchor["page_start"],anchor["page_end"]]]}}})
            actual = PdfSourceService(store.root_dir).fragment(snapshot["stage_id"],source["document_id"],grant,block_id=anchor["block_id"],char_start=anchor["char_start"],char_end=anchor["char_end"])
            matches = record_fingerprint({k:actual[k] for k in ("source","anchor","text")}) == item["fingerprint"]
        except (ValueError, OSError, KeyError): matches = False
        states.append({"source_id":item["source_id"],"matches_current":matches})
    return {"matches_current":stage_ok and all(s["matches_current"] for s in states),"project_matches_current":stage_ok,"sources":states,"observation":"current pinned PDF/parse/anchor comparison; not a grant or edit-history proof"}

