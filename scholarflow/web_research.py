"""Local web adapter for scoped Pi workspaces; domain facts remain in ScholarFlow."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from . import research_workspace as rw, workspace_knowledge as wk
from .domain_queries import DomainQueryError
from .knowledge_jobs import SyncJobStore
from .persistence import atomic_write_json, file_lock
from .pi_lifecycle import node_command

ROOT=Path(__file__).resolve().parents[1]
NATIVE=ROOT/'.tmp/lightrag-native/venv/Scripts/python.exe'
TURN=ROOT/'packages/scholarflow-pi/scripts/web-research-turn.mjs'
ID=re.compile(r'workspace-[a-f0-9]{32}\Z')
TURN_ID=re.compile(r'turn-[a-f0-9]{32}\Z')


def pi_worker_environment():
    """Give a direct desktop launch the same optional Nature home as the CLI."""
    env={**os.environ,'PYTHONUTF8':'1','PYTHONNOUSERSITE':'1','SCHOLARFLOW_PYTHON':sys.executable}
    # An explicit value, including an empty string, is an operator override.
    if 'SCHOLARFLOW_NATURE_HOME' not in env:
        from .nature_skill_package import NaturePackageError, NatureSkillPackage
        default=Path(env['LOCALAPPDATA'])/'ScholarFlow' if env.get('LOCALAPPDATA') else Path.home()/'.scholarflow'
        home=Path(env.get('SCHOLARFLOW_HOME') or default).absolute()
        try:
            if NatureSkillPackage(home).status().get('active'):
                env['SCHOLARFLOW_NATURE_HOME']=str(home)
        except NaturePackageError as error:
            raise RuntimeError('Nature Skill 校验失败；已停止本轮，请检查或回滚已安装版本。') from error
    return env


class WebResearchService:
    """A transport/operation adapter, never a second model or fact database."""
    def __init__(self, data_dir, home=None):
        self.data = rw.safe(Path(data_dir).resolve())
        chosen = Path(home).resolve() if home else self.data / "_research_workspaces"
        self.home = rw.safe(chosen, exists=False)
        self._lock = threading.Lock()
        self._active = set()
        self._indexing = set()

    def projects(self):
        return {"items": [
            {"stage_id": p["stage_id"], "project_name": p.get("project_name", ""), "title": p.get("title", ""), "goal": p.get("goal", "")}
            for p in rw.projects(self.data)
        ]}

    def preview_project(self, payload):
        try:
            return rw.preview_project(
                self.data,
                project_name=payload.get("project_name"),
                title=payload.get("title"),
                goal=payload.get("goal"),
                home=self.home,
            )
        except (ValueError, OSError, TypeError) as error:
            raise DomainQueryError("invalid_argument", str(error)) from error

    def commit_project(self, payload):
        try:
            return rw.commit_project(self.data, payload.get("request_id"), payload.get("reviewed_hash"), home=self.home)
        except (ValueError, OSError, TypeError) as error:
            raise DomainQueryError("invalid_argument", str(error)) from error

    def open_pdf(self, payload):
        stage = payload.get("stage_id")
        pdf = payload.get("pdf_path")
        pages = payload.get("pages") or "1"
        provider = payload.get("provider")
        if not isinstance(stage, str) or not isinstance(pdf, str):
            raise DomainQueryError("invalid_argument", "请选择项目和本机 PDF")
        if not isinstance(provider, str) or provider not in {"deepseek", "scholarflow-local"}:
            raise DomainQueryError("invalid_argument", "请选择受支持的模型")
        try:
            path = rw.prepare(self.data, stage, pdf=pdf, pages=pages, provider=provider, home=self.home)
        except (ValueError, OSError, TypeError) as error:
            raise DomainQueryError("invalid_argument", str(error)) from error
        return self.get(path.parent.name, stage)

    def upload_pdf(self, stage_id, filename, raw, pages="1", provider=None):
        from urllib.parse import unquote
        from uuid import uuid4
        name = Path(unquote(str(filename or ""))).name
        if not isinstance(stage_id, str) or not stage_id.strip():
            raise DomainQueryError("invalid_argument", "请选择项目")
        if not isinstance(provider, str) or provider not in {"deepseek", "scholarflow-local"}:
            raise DomainQueryError("invalid_argument", "请选择受支持的模型")
        if not name.lower().endswith(".pdf") or name.startswith("."):
            raise DomainQueryError("invalid_argument", "请选择 PDF 文件")
        if not isinstance(raw, (bytes, bytearray)) or not 0 < len(raw) <= 80_000_000:
            raise DomainQueryError("invalid_argument", "请上传不超过 80 MB 的 PDF")
        folder = self.home / "uploads" / uuid4().hex
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(bytes(raw))
        return self.open_pdf({"stage_id": stage_id, "pdf_path": str(path), "pages": pages, "provider": provider})

    def read_pdf(self, import_id):
        from .reader_workspace import ReaderWorkspaceStore
        if not isinstance(import_id, str) or not import_id.startswith("pdf_"):
            raise DomainQueryError("invalid_argument", "无效的 PDF")
        reader = ReaderWorkspaceStore(self.data)
        try:
            imported = reader.get_import(import_id)
            payload = reader.pdf_path(import_id).read_bytes()
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise DomainQueryError("not_found", "找不到这份 PDF") from error
        return str(imported.get("filename") or f"{import_id}.pdf"), payload

    def _path(self,workspace_id,stage_id,*,refresh=False):
        if not isinstance(workspace_id,str) or not ID.fullmatch(workspace_id):raise DomainQueryError('invalid_argument','无效的阅读工作区')
        path=self.home/workspace_id/'workspace.json'
        try:m=rw.load(path,refresh=refresh)
        except (ValueError,OSError,KeyError,TypeError) as error:raise DomainQueryError('invalid_state','工作区或来源已变化，请重新选择资料：'+str(error)) from error
        if Path(m['data_dir'])!=self.data or m['stage_id']!=stage_id:raise DomainQueryError('permission_denied','阅读工作区不属于当前项目')
        return path,m

    def create(self,payload):
        stage=payload.get('stage_id');import_id=payload.get('import_id');pages=payload.get('pages','1');provider=payload.get('provider','deepseek')
        if not isinstance(stage,str) or not isinstance(import_id,str) or not isinstance(pages,str):raise DomainQueryError('invalid_argument','请选择项目、PDF及可阅读页码')
        try:path=rw.prepare(self.data,stage,import_id=import_id,pages=pages,provider=provider,home=self.home)
        except (ValueError,OSError,KeyError,TypeError) as error:raise DomainQueryError('invalid_argument',str(error)) from error
        return self.get(path.parent.name,stage)

    def list(self,stage):
        if not isinstance(stage,str) or not any(p['stage_id']==stage for p in self.projects()['items']):raise DomainQueryError('invalid_argument','请先选择有效项目')
        result=[]
        if self.home.exists():
            for path in sorted(self.home.glob('workspace-*/workspace.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:100]:
                try:
                    m=rw.load(path)
                    if Path(m['data_dir'])==self.data and m['stage_id']==stage:
                        import_id=m['pdf']['import_id'] if m['pdf'] else None
                        filename=rw.ReaderWorkspaceStore(self.data).get_import(import_id).get('filename') if import_id else None
                        result.append({'workspace_id':m['workspace_id'],'stage_id':stage,'import_id':import_id,'filename':filename})
                except (ValueError,OSError,KeyError,TypeError):continue
        return {'items':result}

    def get(self,workspace_id,stage):
        path,m=self._path(workspace_id,stage,refresh=True)
        p=m['pdf'];item=rw.ReaderWorkspaceStore(self.data).get_import(p['import_id']) if p else None
        try:proposals=rw.proposals(path) if p else []
        except (ValueError,OSError) as error:raise DomainQueryError('invalid_state',str(error)) from error
        knowledge=wk.status(path) if p else {'status':'not_queued'}
        with self._lock:index_active=workspace_id in self._indexing
        if knowledge.get('status') in {'queued','running'} and not index_active:
            knowledge={**knowledge,'status':'interrupted','error_code':'索引任务未由当前服务管理，请检查后重试。'}
        turns=[];turn_root=path.parent/'web-turns'
        if turn_root.exists():
            for file in sorted(turn_root.glob('turn-*.json'),key=lambda f:f.stat().st_mtime)[-100:]:
                data=json.loads(file.read_text(encoding='utf-8'))
                with self._lock:active=(workspace_id,data['turn_id']) in self._active
                if data['status'] in {'queued','running'} and not active:data={**data,'status':'interrupted'}
                turns.append(data)
        return {'workspace_id':workspace_id,'stage_id':stage,'project_title':m['project_title'],'provider':m['provider'],'knowledge_available':m['knowledge_configured'],
                'pdf':{'import_id':p['import_id'],'filename':item['filename'],'page_count':item['page_count'],
                       'page_ranges':p['page_ranges'],'note_blocks':p['note_blocks'],'total_page_blocks':p['total_page_blocks']} if p else None,
                'knowledge':{k:knowledge.get(k) for k in ('status','error_code','attempts') if k in knowledge},
                'proposals':proposals,'turns':turns}

    def prompt(self,workspace_id,payload):
        stage=payload.get('stage_id');path,m=self._path(workspace_id,stage,refresh=True)
        text=payload.get('message');mode=payload.get('mode','chat');key=payload.get('request_id')
        if not isinstance(text,str) or not text.strip() or len(text)>6000 or mode not in {'chat','note','knowledge'} or not isinstance(key,str) or not TURN_ID.fullmatch(key):
            raise DomainQueryError('invalid_argument','请提供有效的问题和请求标识')
        if mode=='knowledge' and not m['knowledge_configured']:raise DomainQueryError('invalid_state','当前项目尚无已完成的AI知识索引')
        if m['provider']=='deepseek' and payload.get('external_consent') is not True:
            raise DomainQueryError('permission_denied','请先确认授权页和相关项目知识可发送至 DeepSeek')
        directory=path.parent/'web-turns';directory.mkdir(exist_ok=True);file=directory/(key+'.json')
        with self._lock:
            if file.exists():
                prior=json.loads(file.read_text(encoding='utf-8'))
                if prior['message']!=text or prior['mode']!=mode:raise DomainQueryError('invalid_argument','请求标识已用于不同消息')
                return prior
            if any(value[0]==workspace_id for value in self._active):raise DomainQueryError('session_busy','当前 Pi 会话正在处理上一条消息')
            turn={'turn_id':key,'workspace_id':workspace_id,'stage_id':stage,'message':text,'mode':mode,'status':'queued','created_at':time.time()}
            atomic_write_json(file,turn);self._active.add((workspace_id,key))
        threading.Thread(target=self._run_turn,args=(path,m,turn,file),daemon=True,name='sf-web-pi-turn').start()
        return turn

    def _run_turn(self,path,m,turn,file):
        try:
            with file_lock(path.parent/'web-turn',timeout_seconds=1,stale_seconds=900):
                turn['status']='running';atomic_write_json(file,turn)
                stage=m['stage_id'];artifact=m['pdf']['artifact_id'] if m['pdf'] else ''
                prefix=f'当前受控项目 stage_id={stage}，PDF document_id=selected-pdf，笔记 artifact_id={artifact}。请先读取 research-workflow Skill；仅使用当前授权。\n'
                if turn['mode']=='note':
                    prefix+='请读取 pdf-note Skill，先调用 sf_pdf_note_context(mode=create)，只依据其全部授权片段及精确 source_versions 生成一份待审阅的笔记提案；不得批准、写文件或声称已保存。不要增强原文结论，不确定之处明确标注；待审阅、未保存等会变化的操作状态只放在对话说明，不能写进笔记正文。最后简短说明待审阅并给出完整提案ID/hash；正文由网页审阅卡展示，无需重复。\n'
                elif turn['mode']=='knowledge':
                    prefix+='请读取 library-read Skill，并先使用 sf_library_search 检索当前项目；只依据返回的当前证据回答并逐条标注真实 reference_id，缺证据明确说明。\n'
                env=pi_worker_environment()
                completed=subprocess.run([node_command(),str(TURN),'--workspace',str(path)],input=json.dumps({'prompt':prefix+turn['message']},ensure_ascii=False),
                    cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8',timeout=630)
                if completed.returncode:
                    match=re.search(r'tool_error:(sf_[a-z_]+):([a-z_]+)',completed.stderr)
                    if match:raise RuntimeError(f'工具 {match.group(1)} 返回 {match.group(2)}；本轮已停止，请检查来源或参数。')
                    if m['provider']=='scholarflow-local':raise RuntimeError('本地 Qwen 连接或当前资料权限不可用；请检查模型服务和 SSH 隧道，不会自动切换到 DeepSeek。')
                    raise RuntimeError('Pi 会话运行失败，请检查模型连接或当前资料权限')
                output=json.loads(completed.stdout)
                sources=[]
                for item in output.get('sources',[]):
                    origin=item.get('origin_artifact') or {}
                    sources.append({k:item.get(k) for k in ('source_id','reference_id','citation_id','title','text','anchor','evidence_level')}|{'pdf_sources':origin.get('pdf_sources',[])})
                turn.update(status='completed',answer=output['answer'],sources=sources,tool_errors=output.get('tool_errors',[]),usage=output.get('usage',{}),finished_at=time.time())
        except Exception as error:
            turn.update(status='failed',error=str(error) if isinstance(error,RuntimeError) else 'Pi 未能完成本轮；请检查模型连接、来源范围或后重试。',error_type=type(error).__name__,finished_at=time.time())
        finally:
            atomic_write_json(file,turn)
            with self._lock:self._active.discard((turn['workspace_id'],turn['turn_id']))

    def decide(self,workspace_id,payload):
        stage=payload.get('stage_id');path,_=self._path(workspace_id,stage)
        with self._lock:
            if any(value[0]==workspace_id for value in self._active):raise DomainQueryError('session_busy','请先等待 Pi 完成当前消息')
        pid=payload.get('proposal_id');reviewed=payload.get('reviewed_hash');decision=payload.get('decision','approve')
        if not isinstance(pid,str) or not isinstance(reviewed,str) or decision not in {'approve','reject'}:raise DomainQueryError('invalid_argument','请先审阅完整笔记提案')
        try:result=rw.decide(path,pid,reviewed,decision=decision)
        except (ValueError,OSError,KeyError,TypeError) as error:raise DomainQueryError('invalid_state',str(error)) from error
        if decision=='approve' and result.get('phase')=='committed' and result.get('knowledge',{}).get('status')!='failed':
            try:self.index(workspace_id,{'stage_id':stage})
            except Exception as error:
                self._mark_index_failed(result.get('knowledge',{}),error)
                result['knowledge']={'status':'failed','error_code':str(error)[:300],'note_saved':True}
        return {'phase':result.get('phase'),'knowledge':result.get('knowledge',{})}

    def revise(self,workspace_id,payload):
        stage=payload.get('stage_id');path,_=self._path(workspace_id,stage)
        with self._lock:
            if any(value[0]==workspace_id for value in self._active):raise DomainQueryError('session_busy','请先等待 Pi 完成当前消息')
        try:
            result=rw.revise(path,payload.get('proposal_id'),payload.get('reviewed_hash'),
                title=payload.get('title'),content=payload.get('content'),request_id=payload.get('request_id'))
        except (ValueError,OSError,KeyError,TypeError) as error:raise DomainQueryError('invalid_state',str(error)) from error
        return result

    def _set_job_status(self, job, status, error=''):
        try:
            jobs = SyncJobStore(self.data)
            current = jobs.get(job['plan_id'])
            if current['status'] == 'indexed':
                return current
            current.update(status=status, error_code=(error or None), error_type='index_worker' if error else None)
            jobs.save(current)
            return current
        except (KeyError, OSError, ValueError, TypeError):
            return None

    def _mark_index_failed(self, job, error):
        self._set_job_status(job, 'failed', str(error)[:300])

    def index(self, workspace_id, payload):
        stage = payload.get('stage_id')
        path, _ = self._path(workspace_id, stage)
        try:
            job = wk.enqueue(path)
        except (ValueError, OSError, KeyError, TypeError) as error:
            raise DomainQueryError('invalid_state', str(error)) from error
        with self._lock:
            if workspace_id in self._indexing:
                return {'status': 'running', 'plan_id': job['plan_id']}
            self._indexing.add(workspace_id)
        try:
            self._set_job_status(job, 'running')
            threading.Thread(target=self._run_index, args=(path, workspace_id, job), daemon=True, name='sf-web-index').start()
        except Exception as error:
            with self._lock:
                self._indexing.discard(workspace_id)
            self._mark_index_failed(job, error)
            raise DomainQueryError('invalid_state', '知识入库任务无法启动；笔记已保存，可检查后重试。') from error
        return {'status': 'running', 'plan_id': job['plan_id']}

    def _run_index(self,path,workspace_id,job):
        try:
            home=wk.home_for(rw.load(path));home.mkdir(parents=True,exist_ok=True)
            # A library's embedding endpoint and credentials are its own scope.
            # Never copy configuration implicitly from a different library.
            start = self._index_commands(path)
        except Exception as error:
            self._mark_index_failed(job, error)
        finally:
            with self._lock:
                self._indexing.discard(workspace_id)

    def _index_commands(self, path):
        portable = os.environ.get('SCHOLARFLOW_PORTABLE_KNOWLEDGE') == '1'
        if portable:
            if not (wk.home_for(rw.load(path)) / 'embedding-runtime.json').is_file():
                raise RuntimeError('请先显式配置本机 Embedding 服务；不会自动启动或复用未知服务')
        else:
            start = subprocess.run(['powershell', '-NoProfile', '-File', str(ROOT / 'scripts/research-library.ps1'), '-Action', 'StartEmbedding'], cwd=ROOT, capture_output=True, timeout=120)
            if start.returncode:
                raise RuntimeError((start.stderr or b'').decode('utf-8', 'replace')[-300:] or '本机 Embedding 服务未就绪')
        env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONNOUSERSITE': '1'}
        done = subprocess.run([sys.executable if portable else str(NATIVE), '-s', '-B', str(ROOT / 'scripts/workspace-knowledge.py'), 'run', '--workspace', str(path)], cwd=ROOT, env=env, capture_output=True, timeout=900)
        if done.returncode:
            raise RuntimeError((done.stderr or b'').decode('utf-8', 'replace')[-300:] or '知识索引未完成，请检查本机 Embedding 和索引服务')
        return done
