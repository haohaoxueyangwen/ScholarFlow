"""PDF notes reuse existing facts, source snapshots and proposal/CAS governance."""
import importlib.util
from pathlib import Path
import sys

from .mutations import MutationError, record_fingerprint
from .pdf_reading import PdfSourceService
from .source_notes import SourceNoteMutationService, snapshot_references, validate_source_snapshot


def capability_module(name):
    key = 'scholarflow_capability_' + name.replace('-', '_')
    if key not in sys.modules:
        path = Path(__file__).resolve().parents[1] / 'packages/scholarflow-pi/capabilities' / name / 'operations.py'
        spec = importlib.util.spec_from_file_location(key, path)
        module = importlib.util.module_from_spec(spec);sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


def _read_pdf_note_sources(store, stage_id, grant):
    stage = store.get_project_stage(stage_id)
    if stage is None: raise MutationError('not_found', 'PDF note project missing')
    api = capability_module('pdf-read')
    authority, frozen = api.load_pdf_grant(grant.pdf_grant_file)
    if authority.principal != grant.principal: raise MutationError('permission_denied', 'PDF and note principal differ')
    def guard():
        if api.load_pdf_grant(grant.pdf_grant_file)[1] != frozen:
            raise MutationError('permission_denied', 'PDF authority changed during note read')
    service = PdfSourceService(store.root_dir, grant_guard=guard)
    selections = dict(grant.pdf_sources).get(stage_id, ())
    if not selections: raise MutationError('permission_denied', 'No exact PDF fragments granted')
    grouped = {}
    for index, selection in enumerate(selections):
        grouped.setdefault(selection['document_id'], []).append((index, selection))
    actuals = {}
    for document_id, entries in grouped.items():
        fragments = service.fragments(stage_id, document_id, authority,
            [{k:v for k,v in selection.items() if k != 'document_id'} for _, selection in entries])
        actuals.update({index:actual for (index, _), actual in zip(entries, fragments)})
    sources = []
    for index, selection in enumerate(selections):
        actual = actuals[index]
        core = {k:actual[k] for k in ('source', 'anchor', 'text')}
        source_id = 'pdf-source-' + record_fingerprint(selection).removeprefix('sha256:')[:24]
        sources.append({'source_id':source_id, 'fingerprint':record_fingerprint(core), 'source_type':'pdf_fragment', **core})
    snapshot = {'version':2, 'stage_id':stage_id, 'stage_fingerprint':record_fingerprint(stage.to_dict()), 'sources':sources}
    validate_source_snapshot(snapshot);guard()
    return snapshot


def read_pdf_note_sources(store, stage_id, grant):
    from .domain_queries import DomainQueryError
    try:
        return _read_pdf_note_sources(store, stage_id, grant)
    except DomainQueryError as error:
        raise MutationError(error.code, str(error)) from error


class PdfSourceNoteMutationService(SourceNoteMutationService):
    def __init__(self, store, grant):
        super().__init__(store);self.pdf_grant = grant

    def propose_pdf_note(self, artifact_id, *, stage_id, mode, changes, source_versions, quotes=None, request_id=None):
        snapshot = read_pdf_note_sources(self.store, stage_id, self.pdf_grant)
        versions = {s['source_id']:s['fingerprint'] for s in snapshot['sources']}
        if source_versions != versions: raise MutationError('source_version_conflict', 'PDF source versions changed')
        texts = {s['source_id']:s['text'] for s in snapshot['sources']}
        if (quotes is not None and not isinstance(quotes, dict)) or any(k not in texts or v != texts[k] for k,v in (quotes or {}).items()):
            raise MutationError('source_quote_mismatch', 'Quoted excerpts must exactly match the approved fragments')
        def normalize(values, creating):
            result = self._artifact_changes(values, creating)
            if creating: result.update(kind='synthesis', project_stage_id=stage_id)
            papers, refs = snapshot_references(snapshot)
            result.update(source_snapshot=snapshot, related_papers=papers, evidence_refs=refs)
            return result
        return self._propose('research_artifact.publish', 'artifact_id', artifact_id, changes,
            getter=self.store.get_research_artifact, normalizer=normalize,
            existence='create_only' if mode == 'create' else 'update_only', request_id=request_id)

    def _apply_under_lock(self, proposal_id, **kwargs):
        proposal = self.get_proposal(proposal_id)
        current = read_pdf_note_sources(self.store, proposal['normalized_changes']['source_snapshot']['stage_id'], self.pdf_grant)
        if current != proposal['normalized_changes']['source_snapshot']:
            raise MutationError('source_version_conflict', 'PDF authority/source changed before commit')
        return super()._apply_under_lock(proposal_id, **kwargs)
