"""Trusted file-backed PDF CLI. stdout is a single shared-protocol JSON response."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[4]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
for stream in (sys.stdin,sys.stdout,sys.stderr):
    if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-dir',required=True)
    parser.add_argument('--grant-file',required=True)
    parser.add_argument('--audit-path')
    parser.add_argument('--request-json')
    parser.add_argument('--status',action='store_true')
    args=parser.parse_args()
    request=None
    try:
        request=json.loads(args.request_json) if args.request_json else None
        from operations import build_pdf_facade,read_grant
        grant=read_grant(args.grant_file)
        facade=build_pdf_facade(args.data_dir,args.grant_file,args.audit_path)
        if args.status:
            print(json.dumps(facade.describe(grant),ensure_ascii=False));return 0
        request=request if request is not None else json.load(sys.stdin)
        response=facade.execute(request,grant)
    except Exception as error:
        from scholarflow.domain_queries import DomainQueryError
        identity=request if isinstance(request,dict) else {}
        code=error.code if isinstance(error,DomainQueryError) else 'dependency_unavailable' if isinstance(error,ImportError) else 'invalid_argument'
        response={'protocol_version':1,**{k:identity.get(k) for k in ('request_id','task_id','operation','scope')},'ok':False,'status':'error','source_versions':[],
                  'error':{'code':code,'message':'PDF adapter configuration or request is invalid/unavailable','retryable':False}}
        response['receipt']={**{k:response[k] for k in ('request_id','task_id','operation')},'execution':'not_started','read_only':True,'audit':{'persisted':False},'outcome':'error'}
    print(json.dumps(response,ensure_ascii=False))
    return 0 if response['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
