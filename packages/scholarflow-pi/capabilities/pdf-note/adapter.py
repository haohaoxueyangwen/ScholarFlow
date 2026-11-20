import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[4]));sys.path.insert(0,str(Path(__file__).resolve().parent))
for stream in (sys.stdin,sys.stdout,sys.stderr):
    if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',required=True);parser.add_argument('--grant-file',required=True);parser.add_argument('--request-json');parser.add_argument('--operator',action='store_true');parser.add_argument('--status',action='store_true');args=parser.parse_args();request=None
    try:
        from operations import PdfNoteGrant,PdfNoteFacade
        request=json.loads(args.request_json) if args.request_json else None
        grant=PdfNoteGrant.from_dict(json.loads(Path(args.grant_file).read_text(encoding='utf-8-sig')));facade=PdfNoteFacade(args.data_dir,grant)
        if args.status:print(json.dumps(facade.describe(grant),ensure_ascii=False));return 0
        request=request if request is not None else json.load(sys.stdin);response=facade.execute(request,grant,operator=args.operator)
    except Exception:
        identity=request if isinstance(request,dict) else {};response={'protocol_version':1,**{k:identity.get(k) for k in ('request_id','task_id','operation','scope')},'ok':False,'status':'error','source_versions':[],'error':{'code':'invalid_argument','message':'Invalid PDF note transport or host configuration','retryable':False},'receipt':{'execution':'not_started','domain_write':'not_attempted'}}
    print(json.dumps(response,ensure_ascii=False));return 0 if response['ok'] else 1
if __name__=='__main__':raise SystemExit(main())
