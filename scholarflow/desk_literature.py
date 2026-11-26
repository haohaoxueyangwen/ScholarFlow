"""Bounded, project-scoped scholarly search and open PDF handoff."""
from __future__ import annotations

from hashlib import sha256
from threading import BoundedSemaphore, Lock
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

from . import research_workspace as rw
from .domain_queries import DomainQueryError
from .literature_search import LiteratureSearchError, ResilientLiteratureSearch
from .remote_pdf import RemotePdfError, fetch_remote_pdf


class DeskLiterature:
    def __init__(self, *, searcher=None, fetcher=None):
        self.searcher = searcher if searcher is not None else ResilientLiteratureSearch()
        self.fetcher = fetcher if fetcher is not None else fetch_remote_pdf
        self._guard = Lock()
        self._search_slot = BoundedSemaphore(1)
        self._download_slot = BoundedSemaphore(1)
        self._candidates = {}

    def search(self, stage, query, page):
        if not isinstance(query, str) or not 2 <= len(' '.join(query.split())) <= 400 or type(page) is not int or not 1 <= page <= 10:
            raise DomainQueryError('invalid_argument', '输入 2–400 个字符的关键词或 DOI。')
        if not self._search_slot.acquire(blocking=False):
            raise DomainQueryError('session_busy', '已有文献检索正在运行，请稍候重试。')
        try:
            result = self.searcher.search(query, limit=12, **({'page': page} if page > 1 else {}))
        except LiteratureSearchError as error:
            raise DomainQueryError(error.code, '文献服务暂不可用或已限流，请稍后重试。') from error
        finally:
            self._search_slot.release()
        metadata = getattr(result, 'metadata', {})
        items, seen = [], set()
        now = monotonic()
        with self._guard:
            self._candidates = {key: value for key, value in self._candidates.items() if value['expires'] > now}
            for hit in result[:12]:
                identity = (hit.get('doi') or hit.get('result_id') or hit.get('title', '')).casefold()
                if not identity or identity in seen:
                    continue
                seen.add(identity)
                key = uuid4().hex
                open_url = hit.get('open_pdf_url', '') if hit.get('provider') == 'openalex' else ''
                self._candidates[key] = {'stage': stage, 'expires': now + 900, 'hit': hit, 'open_url': open_url, 'completed': {}}
                items.append({**{k: hit.get(k) for k in ('title', 'authors', 'year', 'venue', 'doi', 'provider', 'primary_url')},
                              'candidate_id': key, 'pdf_available': bool(open_url)})
            while len(self._candidates) > 240:
                self._candidates.pop(next(iter(self._candidates)))
        return {'items': items, 'page': page, 'has_more': bool(page < 10 and metadata.get('has_more')),
                'provider': metadata.get('provider') or (items[0]['provider'] if items else '')}

    def open(self, data, stage, home, candidate_id, pages, provider):
        if not isinstance(candidate_id, str) or not isinstance(pages, str) or len(pages) > 160 or not isinstance(provider, str) or provider not in {'deepseek', 'scholarflow-local'}:
            raise DomainQueryError('invalid_argument', '请选择结果、模型和可读页。')
        try:
            normalized = rw.page_ranges(pages, 100_000)
        except (ValueError, TypeError) as error:
            raise DomainQueryError('invalid_argument', '可读页格式应为 1、1-3 或 1,3。') from error
        signature = (tuple(map(tuple, normalized)), provider)
        with self._guard:
            candidate = self._candidates.get(candidate_id)
            if not candidate or candidate['stage'] != stage or candidate['expires'] <= monotonic():
                raise DomainQueryError('not_found', '结果已失效或不属于当前项目，请重新搜索。')
            open_url, hit = candidate['open_url'], candidate['hit']
            if not open_url:
                raise DomainQueryError('source_unavailable', '这条结果只有题录，没有可确认的开放 PDF。')
            if signature in candidate['completed']:
                return candidate['completed'][signature]
        if not self._download_slot.acquire(blocking=False):
            raise DomainQueryError('session_busy', '正在加入另一份 PDF，请稍后重试。')
        try:
            with self._guard:
                if signature in candidate['completed']:
                    return candidate['completed'][signature]
            try:
                raw, _, final_url = self.fetcher(open_url, max_bytes=rw.MAX_READER_PDF_BYTES)
            except RemotePdfError as error:
                raise DomainQueryError(error.code, '未取得有效公开 PDF；可以打开题录，或手动上传文件。') from error
            reader = rw.ReaderWorkspaceStore(data)
            origin = {'source': 'remote_open_pdf', 'doi': hit.get('doi'), 'source_host': urlsplit(final_url).hostname or '',
                      'source_url_sha256': sha256(final_url.encode()).hexdigest()}
            bibliography = {k: hit.get(k) for k in ('title', 'doi', 'authors', 'year')}
            filename = (hit.get('title') or 'Open access paper')[:100] + '.pdf'
            record = reader.import_pdf(filename, raw, origin=origin, bibliography=bibliography)
            if record.get('status') != 'parsed':
                raise DomainQueryError('source_unavailable', '已下载 PDF，但未解析出可授权的正文；暂未建立阅读会话。')
            if any(last > record['page_count'] for _, last in normalized):
                raise DomainQueryError('invalid_argument', f'这份 PDF 只有 {record["page_count"]} 页，请调整 Pi 可读页。')
            reader.import_pdf(filename, raw, origin=origin, bibliography=bibliography, project_stage_ids=[stage])
            try:
                path = rw.prepare(data, stage, import_id=record['import_id'], pages=pages, provider=provider, home=home)
            except (OSError, ValueError, TypeError) as error:
                raise DomainQueryError('invalid_state', 'PDF 已加入资料，但未能建立阅读会话：' + str(error)) from error
            workspace = path.parent.name
            with self._guard:
                candidate['completed'][signature] = workspace
            return workspace
        finally:
            self._download_slot.release()
