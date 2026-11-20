"""Human-operated workspace menu; all language generation runs in native Pi."""
import argparse,json,os,subprocess,sys
from pathlib import Path
for stream in (sys.stdout,sys.stderr):
    if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scholarflow import research_workspace as rw
from scholarflow.pi_lifecycle import node_command

def sync_knowledge(path):
    from scholarflow import workspace_knowledge as wk
    job=None
    try:
        job=wk.enqueue(path)
        print('笔记已保存；正在更新当前项目AI知识库（仅本机Embedding，无文本模型调用）……',flush=True)
        ready=subprocess.run(['powershell','-NoProfile','-File',str(ROOT/'scripts/research-library.ps1'),'-Action','StartEmbedding'],cwd=ROOT,check=False,timeout=120)
        if ready.returncode:raise ValueError('本机Embedding未就绪')
        home=wk.home_for(rw.load(path));runtime=ROOT/'.tmp/lightrag-native/venv/Scripts/python.exe'
        config=rw.DEFAULT_DATA.parent/'embedding-runtime.json'
        if home!=rw.DEFAULT_DATA.parent and config.exists() and not (home/'embedding-runtime.json').exists():
            from scholarflow.persistence import atomic_write_json
            atomic_write_json(home/'embedding-runtime.json',json.loads(config.read_text(encoding='utf-8')))
        done=subprocess.run([str(runtime),'-s','-B',str(ROOT/'scripts/workspace-knowledge.py'),'run','--workspace',str(path)],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',env={**os.environ,'PYTHONUTF8':'1','PYTHONNOUSERSITE':'1'},timeout=900)
        if done.returncode:raise ValueError(done.stderr[-500:])
        print('AI知识库已更新，下一次打开Pi聊天即可检索当前项目笔记。')
        return json.loads(done.stdout)
    except (ValueError,OSError,subprocess.TimeoutExpired) as error:
        if job:
            from scholarflow.knowledge_jobs import SyncJobStore
            jobs=SyncJobStore(Path(rw.load(path)['data_dir']));current=jobs.get(job['plan_id'])
            if current['status'] not in {'indexed','running'}:
                current.update(status='failed',error_code=str(error)[:300]);jobs.save(current)
        print(('笔记已保存，AI索引待重试：' if job else '尚无有效已保存笔记，未开始入库：')+str(error));return {'status':'failed','note_saved':bool(job)}

def review(path):
    items=rw.proposals(path)
    if not items:print('还没有笔记提案；在 Pi 里要求“为选定片段生成笔记提案”。');return
    for item in items:
        print('\n状态：'+item['phase']);print(json.dumps(item['proposal']['normalized_changes'],ensure_ascii=False,indent=2));print('精确内容hash：'+item['proposal_hash'])
        if item['phase'] in {'pending','approved_pending'} and input('输入 SAVE 确认保存这份领域笔记；其他输入跳过：').strip()=='SAVE':
            rw.decide(path,item['proposal']['proposal_id'],item['proposal_hash']);print('领域笔记已保存。');sync_knowledge(path)
        current=next(x for x in rw.proposals(path) if x['proposal']['proposal_id']==item['proposal']['proposal_id'])
        if current['phase']=='committed':
            plan=rw.export(path);print(json.dumps(plan,ensure_ascii=False,indent=2))
            if plan['phase']!='committed' and input('核对上面的目标文件/冻结内容，输入 EXPORT 确认本地导出：').strip()=='EXPORT':
                rw.export(path,reviewed_hash=plan['proposal_hash']);print('本地文件已导出；AI索引状态单独显示，未写Obsidian。')

def menu(data):
    while True:
        action=input('\nScholarFlow 日用入口\n 1 选择项目，开始新的阅读工作区\n 2 恢复已有工作区\n 3 按研究目标创建项目\n 0 退出\n选择：').strip()
        if action=='0':return
        try:
            if action=='3':
                preview=rw.preview_project(data,project_name=input('项目名称：').strip(),title=input('当前阶段标题：').strip(),goal=input('研究目标：').strip())
                print(json.dumps({'stage_id':preview['stage_id'],'proposal_hash':preview['proposal_hash'],'changes':preview['changes']},ensure_ascii=False,indent=2))
                print('项目尚未创建。输入上面的精确 hash 才会写入；其他输入取消。')
                typed=input('精确 hash：').strip()
                if typed!=preview['proposal_hash']:
                    print('未创建：hash 不一致。');continue
                created=rw.commit_project(data,preview['request_id'],typed)
                print('已创建项目 '+created['project_name']+' ['+created['stage_id']+']。可以选 1 开始阅读。')
                continue
            if action=='1':
                records=rw.projects(data)
                if not records:print('当前没有科研项目。请选 3 按研究目标创建，确认精确 hash 后才会写入。');continue
                for i,p in enumerate(records,1):print(f"{i}. {p.get('project_name','')} / {p.get('title','')} [{p['stage_id']}]")
                choice=int(input('项目序号：'))
                if not 1<=choice<=len(records):raise ValueError('请选择列表内的项目')
                stage=records[choice-1]['stage_id'];pdf=input('PDF完整路径（可拖入；留空只查现有项目知识）：').strip().strip('"')
                pages=input('允许阅读/发送给所选模型的页码，例如1-3,5（默认1）：').strip() or '1' if pdf else '1'
                provider='scholarflow-local' if input('模型：1 DeepSeek API（默认），2 现有Qwen；不自动启动模型服务：').strip()=='2' else 'deepseek'
                print('笔记来源默认选这些页的前8个完整文本块，少于8个则全部；不会声称全文。')
                if input('输入 START 确认所选项目、文件/页范围及模型；其他输入返回：').strip()!='START':continue
                path=rw.prepare(data,stage,pdf=pdf or None,pages=pages,provider=provider)
            elif action=='2':
                candidates=sorted(rw.HOME.glob('workspace-*/workspace.json'),key=lambda p:p.stat().st_mtime,reverse=True) if rw.HOME.exists() else []
                for i,p in enumerate(candidates,1):
                    try:m=rw.load(p,refresh=True);print(f"{i}. {m['project_title']} / {m['provider']} [{p.parent.name}]")
                    except ValueError:print(f'{i}. 已失效，需重新选择资料 [{p.parent.name}]')
                if not candidates:print('暂无工作区。');continue
                choice=int(input('工作区序号：'))
                if not 1<=choice<=len(candidates):raise ValueError('请选择列表内的工作区')
                path=candidates[choice-1];rw.load(path,refresh=True)
            else:continue
            while True:
                m=rw.load(path,refresh=True);print(f"\n项目：{m['project_title']}\n工作区：{path.parent}\n现有项目知识：{'已配置，连接以实际检索结果为准' if m['knowledge_configured'] else '未配置；PDF阅读仍可用'}")
                if m['pdf']:
                    from scholarflow import workspace_knowledge as wk
                    state=wk.status(path);print(f"PDF页范围：{m['pdf']['page_ranges']}；笔记来源块：{m['pdf']['note_blocks']}/{m['pdf']['total_page_blocks']}；AI索引：{state['status']}")
                    if state.get('error_code'):print('入库错误：'+state['error_code'])
                option=input('1 打开/继续Pi聊天；2 审阅保存/本地导出；3 恢复/重试当前笔记AI入库；0 返回项目菜单：').strip()
                if option=='0':break
                if option=='1':
                    result=subprocess.run([node_command(),str(ROOT/'packages/scholarflow-pi/scripts/research-chat.mjs'),'--workspace',str(path)],cwd=ROOT,check=False)
                    if result.returncode:print('Pi未正常完成，请保留错误；不会自动换模型或重试。')
                elif option=='2':review(path)
                elif option=='3':sync_knowledge(path)
        except (ValueError,OSError,IndexError) as error:print('未执行或已停止：'+str(error))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['menu','projects','prepare','check','proposals','decide','export','preview-project','commit-project']);parser.add_argument('--data-dir',default=str(rw.DEFAULT_DATA));parser.add_argument('--stage');parser.add_argument('--pdf');parser.add_argument('--pages',default='1');parser.add_argument('--provider',choices=['deepseek','scholarflow-local'],default='deepseek');parser.add_argument('--workspace');parser.add_argument('--proposal-id');parser.add_argument('--reviewed-hash');parser.add_argument('--name');parser.add_argument('--title');parser.add_argument('--goal');parser.add_argument('--request-id');args=parser.parse_args()
    if args.action=='menu':menu(Path(args.data_dir));return
    if args.action=='projects':result=rw.projects(args.data_dir)
    elif args.action=='preview-project':result=rw.preview_project(args.data_dir,project_name=args.name,title=args.title,goal=args.goal)
    elif args.action=='commit-project':result=rw.commit_project(args.data_dir,args.request_id,args.reviewed_hash)
    elif args.action=='prepare':result={'workspace':str(rw.prepare(args.data_dir,args.stage,pdf=args.pdf,pages=args.pages,provider=args.provider))}
    elif args.action=='check':result=rw.load(args.workspace,refresh=True)
    elif args.action=='proposals':result=rw.proposals(args.workspace)
    elif args.action=='decide':
        if not args.proposal_id or not args.reviewed_hash:raise ValueError('Explicit proposal ID and reviewed hash required')
        result=rw.decide(args.workspace,args.proposal_id,args.reviewed_hash)
        if result['phase']=='committed':result['knowledge']=sync_knowledge(args.workspace)
    else:result=rw.export(args.workspace,reviewed_hash=args.reviewed_hash)
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':
    try:main()
    except (KeyboardInterrupt,EOFError):print('\n已退出；既有Pi会话和提案保留，不自动重试。')
    except (ValueError,OSError,TypeError) as error:print(str(error),file=sys.stderr);raise SystemExit(1)
