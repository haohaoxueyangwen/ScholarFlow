from dataclasses import dataclass
from pathlib import Path

from scholarflow.governed_writes import GovernedWriteFacade, text
from scholarflow.pdf_notes import capability_module, read_pdf_note_sources, PdfSourceNoteMutationService
from scholarflow.read_operations import ReadOperationError
from scholarflow.source_notes import snapshot_references, validate_source_snapshot, source_version_state, MAX_SOURCES

base = capability_module('note-write')


@dataclass(frozen=True)
class PdfNoteGrant:
    principal: str
    create_notes: tuple
    update_notes: tuple
    pdf_grant_file: str
    pdf_sources: tuple

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {'principal','create_notes','update_notes','pdf_grant_file','pdf_sources'}:
            raise ReadOperationError('invalid_argument','Exact PDF note grant required')
        prior = base.NoteWriteGrant.from_dict({k:value[k] for k in ('principal','create_notes','update_notes')} | {'source_papers':{}})
        file = value['pdf_grant_file']
        if not isinstance(file,str) or not Path(file).is_absolute(): raise ReadOperationError('invalid_argument','Absolute host PDF grant file required')
        mapping=value['pdf_sources']
        if not isinstance(mapping,dict) or not 1 <= len(mapping) <= 100: raise ReadOperationError('invalid_argument','Bounded PDF note sources required')
        sources=[]
        for stage,items in mapping.items():
            base.identifier(stage,'stage')
            if not isinstance(items,list) or not 1 <= len(items) <= MAX_SOURCES: raise ReadOperationError('invalid_argument','Bounded exact fragment selectors required')
            for item in items:
                if not isinstance(item,dict) or set(item) != {'document_id','block_id','char_start','char_end'} or any(not isinstance(item[k],str) or not item[k] for k in ('document_id','block_id')) or any(type(item[k]) is not int for k in ('char_start','char_end')) or not 0 <= item['char_start'] < item['char_end']:
                    raise ReadOperationError('invalid_argument','Exact PDF fragment selector required')
            sources.append((stage,tuple(dict(i) for i in items)))
        return cls(prior.principal,prior.create_notes,prior.update_notes,file,tuple(sources))

    authorize = base.NoteWriteGrant.authorize


class PdfNoteDomain(base.NoteDomain):
    capability, prefix = 'pdf-note', 'pdf.note.'
    def __init__(self, grant): self.grant=grant
    def read_context(self,store,grant,scope,mode):
        snapshot=read_pdf_note_sources(store,scope['stage_id'],grant)
        return {'project_stage_id':scope['stage_id'],'artifact_id':scope['artifact_id'],'sources':snapshot['sources'],'project_fingerprint':snapshot['stage_fingerprint'],'source_kind':'verified_pdf_fragments','ai_index_updated':False}
    def service(self,store): return PdfSourceNoteMutationService(store,self.grant)
    def propose(self,service,grant,scope,mode,changes,request_id):
        return service.propose_pdf_note(scope['artifact_id'],stage_id=scope['stage_id'],mode=mode,request_id=request_id,**changes)
    def prepare_changes(self,grant,scope,params):
        if set(params) - {'mode','artifact_id','changes','source_versions','expected_fingerprint','quotes'} or not {'mode','artifact_id','changes','source_versions','expected_fingerprint'} <= set(params) or params['artifact_id'] != scope['artifact_id'] or params['mode'] not in ('create','update'):
            raise ReadOperationError('invalid_argument','Explicit PDF note mode/fields/source versions/base required')
        grant.authorize(scope,params['mode'])
        changes=base.validate_note_changes(params['changes'])
        if 'status' in changes and changes['status'] not in ('draft','reviewed','final','archived'):raise ReadOperationError('invalid_argument','Invalid note status')
        versions=params['source_versions']
        if not isinstance(versions,dict) or not 1 <= len(versions) <= MAX_SOURCES: raise ReadOperationError('invalid_argument','Exact PDF source versions required')
        return {'changes':dict(changes),'source_versions':dict(versions),'quotes':params.get('quotes',{})}
    def validate_proposal(self,store,grant,scope,proposal):
        if proposal.get('operation')!='research_artifact.publish' or proposal.get('target')!={'artifact_id':scope['artifact_id']} or proposal.get('mode') not in ('create','update'):
            raise ReadOperationError('scope_conflict','Proposal does not match exact PDF note')
        grant.authorize(scope,proposal['mode']);self.validate_current(store,scope)
        changes=proposal['normalized_changes'];snapshot=changes.get('source_snapshot');validate_source_snapshot(snapshot)
        if snapshot['version']!=2 or snapshot['stage_id']!=scope['stage_id'] or set(changes)- (base.FIELDS|{'source_snapshot','related_papers','evidence_refs','kind','project_stage_id'}):raise ReadOperationError('scope_conflict','Invalid PDF note proposal fields')
        if proposal['mode']=='create' and (changes.get('kind')!='synthesis' or changes.get('project_stage_id')!=scope['stage_id']):raise ReadOperationError('scope_conflict','PDF note project differs')
        papers,refs=snapshot_references(snapshot)
        if changes.get('related_papers')!=papers or changes.get('evidence_refs')!=refs:raise ReadOperationError('scope_conflict','PDF source references differ')
        # Revalidate current authority even for a pending proposal/status call.
        current=read_pdf_note_sources(store,scope['stage_id'],grant)
        if [s['source_id'] for s in current['sources']] != [s['source_id'] for s in snapshot['sources']]:raise ReadOperationError('permission_denied','PDF note selectors revoked')


class PdfNoteFacade(GovernedWriteFacade):
    def __init__(self,data_dir,grant):super().__init__(data_dir,PdfNoteDomain(grant))
