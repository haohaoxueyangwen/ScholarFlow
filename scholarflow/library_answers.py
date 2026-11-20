"""Render compact Pi-authored claims with service-owned citation expansion."""
from .domain_queries import DomainQueryError

def render_claims(payload,items):
    if isinstance(payload,dict) and set(payload)=={'claims','abstention'} and payload['claims']==[] and isinstance(payload['abstention'],str) and 1<=len(payload['abstention'].strip())<=600:
        return {'markdown':'当前证据不足：'+payload['abstention'].strip()+'\n\n此结论只表示本次授权检索片段不足，不表示整库或原论文没有该信息。','references':[],'claims':[],'status':'insufficient_evidence','semantic_entailment':'requires independent review'}
    if not isinstance(payload,dict) or set(payload)!={'claims'} or not isinstance(payload['claims'],list) or not 1<=len(payload['claims'])<=8:
        raise DomainQueryError('invalid_answer','One to eight cited claims required')
    catalog={}
    for item in items:
        key=item['citation_id']
        if key in catalog and catalog[key]['citation']!=item['citation']:raise DomainQueryError('invalid_citation','Citation handle collision')
        catalog[key]=item
    lines=[];used={}
    for claim in payload['claims']:
        if not isinstance(claim,dict) or set(claim)!={'text','citations'} or not isinstance(claim['text'],str) or not claim['text'].strip() or len(claim['text'])>600 or not isinstance(claim['citations'],list) or not 1<=len(claim['citations'])<=6 or any(not isinstance(c,str) or c not in catalog for c in claim['citations']):raise DomainQueryError('invalid_citation','Claims must cite returned evidence handles')
        for key in claim['citations']:used.setdefault(key,len(used)+1)
        lines.append(claim['text']+' '+''.join('['+str(used[c])+']' for c in claim['citations']))
    lines.extend(['','## 来源'])
    references=[]
    for key,index in used.items():
        item=catalog[key];cite=item['citation'];anchor=cite['anchor'];origin=item.get('authorship',{'kind':'unknown'})
        label='已记录模型派生笔记' if origin['kind']=='recorded_model_derivation' else '既有资料，作者来源未知'
        location=('PDF页'+str(anchor['page_start'])+'，块'+anchor['block_id']) if 'block_id' in anchor else anchor.get('note_file','source')
        lines.append(f"[{index}] {item['title']}；{label}；{location}，字符 {anchor['char_start']}–{anchor['char_end']}；source_id={cite['source_id']}；reference_id={cite['reference_id']}")
        references.append({'number':index,**cite,'authorship':origin})
    lines.extend(['','检索后端未调用生成模型；以上归纳由 Pi 模型生成。引用有效性核验不等于原论文科学结论已获独立验证。'])
    return {'markdown':'\n\n'.join(lines),'references':references,'claims':payload['claims'],'semantic_entailment':'requires independent review'}
