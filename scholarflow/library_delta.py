"""Pure, reviewable source delta; no scan, model call or write."""
def build_delta(catalog,group_entries,project_entries):
    current={r['source_id']:r for r in catalog if r['kind']=='md' and not r.get('duplicate_of')}
    upsert=[]
    for key,record in current.items():
        group=group_entries.get(key,{});project=project_entries.get(key,{})
        if any(entry.get('sha256')!=record['sha256'] or entry.get('revoked') for entry in (group,project)):upsert.append(key)
    revoke=sorted(key for key,entry in project_entries.items() if key not in current and not entry.get('revoked'))
    return {'upsert':sorted(upsert),'revoke':revoke,'versions':{key:current[key]['sha256'] for key in sorted(current)},'allow_physical_deletes':False,'pdf_policy':'New PDF full reading requires a separately frozen Pi reading batch'}
