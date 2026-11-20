"""One bounded derived-index operation. No language-model client."""
import argparse,asyncio,json,os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
for stream in (sys.stdout,sys.stderr):
    if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
from scholarflow import research_workspace as rw
from scholarflow import workspace_knowledge as wk

def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['status','enqueue','run']);parser.add_argument('--workspace',required=True);args=parser.parse_args()
    m=rw.load(args.workspace);os.environ['SCHOLARFLOW_LIBRARY_HOME']=str(wk.home_for(m));os.environ['SCHOLARFLOW_LIBRARY_FACTS_DIR']=m['data_dir']
    result=wk.status(args.workspace) if args.action=='status' else wk.enqueue(args.workspace) if args.action=='enqueue' else asyncio.run(wk.process(args.workspace))
    if args.action=='run':rw.load(args.workspace,refresh=True)
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':
    try:main()
    except Exception as error:print(str(error),file=sys.stderr);raise SystemExit(1)
