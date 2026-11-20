"""One shared read-protocol request; no session store or model runtime."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[4]))
for stream in (sys.stdout,sys.stderr):
    if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',required=True);parser.add_argument('--grant-file',required=True);parser.add_argument('--request-json');parser.add_argument('--audit-path');parser.add_argument('--status',action='store_true');args=parser.parse_args();request={}
    try:
        from operations import read_grant,build_facade
        grant=read_grant(args.grant_file);facade=build_facade(args.data_dir,args.grant_file,args.audit_path)
        if args.status:print(json.dumps(facade.describe(grant)|{'checkpoint_store':'Pi native custom entries','research_facts_written':False}));return 0
        request=json.loads(args.request_json);result=facade.execute(request,grant)
    except Exception as error:
        result={'protocol_version':1,**{k:request.get(k) for k in ('request_id','task_id','operation','scope')},'ok':False,'status':'error','source_versions':[],'error':{'code':getattr(error,'code','invalid_argument'),'message':'Continuity request/configuration invalid','retryable':False}}
        result['receipt']={**{k:result[k] for k in ('request_id','task_id','operation')},'read_only':True,'execution':'not_started','audit':{'persisted':False}}
    print(json.dumps(result,ensure_ascii=False));return 0 if result['ok'] else 1
if __name__=='__main__':raise SystemExit(main())
