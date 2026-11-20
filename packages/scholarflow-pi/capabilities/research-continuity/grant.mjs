export function validateContinuityGrant(g){
 const fields=['principal','stage_id','workflow_id','objective','library_grant_file','source_ids'];
 return !!g&&typeof g==='object'&&!Array.isArray(g)&&Object.keys(g).length===fields.length&&fields.every(k=>k in g)
  &&typeof g.principal==='string'&&g.principal.trim()===g.principal&&g.principal.length>0&&g.principal.length<=160
  &&typeof g.stage_id==='string'&&/^stage-[A-Za-z0-9_-]{1,160}$/.test(g.stage_id)&&!g.stage_id.startsWith('stage-library-')
  &&typeof g.workflow_id==='string'&&/^[A-Za-z0-9_-]{1,160}$/.test(g.workflow_id)
  &&typeof g.objective==='string'&&g.objective.trim()===g.objective&&g.objective.length>0&&g.objective.length<=2000
  &&typeof g.library_grant_file==='string'&&g.library_grant_file.length>0
  &&Array.isArray(g.source_ids)&&g.source_ids.length>0&&g.source_ids.length<=100&&new Set(g.source_ids).size===g.source_ids.length&&g.source_ids.every(x=>typeof x==='string'&&/^source-[a-f0-9]{24}$/.test(x));
}
