from scholarflow.research_continuity import load_grant,observe
from scholarflow.read_operations import Field,Operation,ReadFacade,ReadGrant

def read_grant(path):
    value,_=load_grant(path);return ReadGrant(value['principal'],frozenset({'research-continuity'}),stage_ids=frozenset({value['stage_id']}))
def build_facade(data_dir,grant_file,audit_path=None):
    def handler(snapshot,params,control,authority):
        value,_=load_grant(grant_file)
        if value['principal']!=authority.principal:
            from scholarflow.domain_queries import DomainQueryError
            raise DomainQueryError('permission_denied','Continuity principal changed')
        return observe(data_dir,grant_file,params['stage_id'],params['source_ids'],purpose=params['purpose'],expected=params.get('expected'),control=control)
    fields={'stage_id':Field(str,True),'source_ids':Field(list,True),'purpose':Field(str,True),'expected':Field(dict)}
    operation=Operation('continuity.observe','research-continuity','project_stage',fields,lambda *_:None,stage_field='stage_id',resources=('project_stages.json',),context_handler=handler)
    return ReadFacade([operation],data_dir,audit_path)
