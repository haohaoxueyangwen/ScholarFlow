"""Capability-owned PDF operations over the shared read protocol; no model loop."""
import json
from pathlib import Path

from scholarflow.domain_queries import DomainQueryError
from scholarflow.mutations import record_fingerprint
from scholarflow.pdf_reading import PdfReadGrant, PdfSourceService
from scholarflow.read_operations import Field, Operation, ReadFacade, ReadGrant


def load_pdf_grant(path):
    path = Path(path).absolute()
    try:
        if path.resolve() != path or any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in (path, *path.parents)):
            raise ValueError('Aliased grant')
        if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 65536:
            raise ValueError('Unbounded or non-regular grant')
        raw = path.read_bytes()
        if len(raw) > 65536:
            raise ValueError('Grant grew')
        grant = PdfReadGrant.from_dict(json.loads(raw.decode('utf-8-sig')))
    except (OSError, ValueError, UnicodeError):
        raise DomainQueryError('permission_denied', 'Current host PDF grant is unavailable or invalid') from None
    return grant, raw


def read_grant(path):
    grant, _ = load_pdf_grant(path)
    return ReadGrant(grant.principal, frozenset({'pdf-read'}), stage_ids=frozenset(d['stage_id'] for d in grant.documents.values()))


def build_pdf_facade(data_dir, grant_file, audit_path=None):
    root = Path(data_dir).absolute()
    def handler(method):
        def execute(snapshot, params, control, authority):
            grant, approved = load_pdf_grant(grant_file)
            if grant.principal != authority.principal:
                raise DomainQueryError('permission_denied', 'Host PDF principal changed')
            def guard():
                control.check()
                _, current = load_pdf_grant(grant_file)
                if current != approved:
                    raise DomainQueryError('permission_denied', 'Host PDF grant changed during read')
            service = PdfSourceService(root, grant_guard=guard)
            fields = {k:v for k,v in params.items() if k not in ('stage_id','document_id')}
            result = getattr(service, method)(params['stage_id'], params['document_id'], grant, **fields)
            stage = snapshot.store.get_project_stage(params['stage_id'])
            if stage is None or record_fingerprint(stage.to_dict()) != result['source']['stage_fingerprint']:
                raise DomainQueryError('source_version_conflict', 'Project changed during PDF read')
            for kind, key in (('original','content_hash'),('structure','structure_hash')):
                resource = 'pdf:' + params['document_id'] + ':' + kind
                snapshot.versions[resource] = {'resource':resource,'present':True,'sha256':result['source'][key].removeprefix('sha256:')}
            guard()
            return result
        return execute
    common={'stage_id':Field(str,True),'document_id':Field(str,True)}
    operations=[
        Operation('pdf.inspect','pdf-read','project_stage',common | {'offset':Field(int,minimum=0),'limit':Field(int,minimum=1,maximum=100)},
                  lambda *_:None,stage_field='stage_id',resources=('project_stages.json',),context_handler=handler('inspect')),
        Operation('pdf.fragment','pdf-read','project_stage',common | {'block_id':Field(str,True),'char_start':Field(int,minimum=0),'char_end':Field(int,minimum=1)},
                  lambda *_:None,stage_field='stage_id',resources=('project_stages.json',),context_handler=handler('fragment')),
    ]
    return ReadFacade(operations,root,audit_path)
