"""Single-owner mobile entry. Run via uvicorn web_app:app; worker is separate."""
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from note_input import parse_note
from collector import video_name
from service_check import readiness, worker_alive
from web_store import DraftStore, DraftConflict
from runtime_config import load_runtime_config

HERE = Path(__file__).resolve().parent
COOKIE = 'collector_session'


def load_config(path):
    config = load_runtime_config(path)
    root = Path(config.get('data_dir', 'data'))
    if not root.is_absolute():
        root = Path(path).resolve().parent / root
    return config, root


class Login(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class Share(BaseModel):
    id: UUID
    text: str = Field(min_length=1, max_length=10000)


class Note(BaseModel):
    note: str = Field(max_length=4000)


class DraftNote(Note):
    version: int = Field(ge=0, strict=True)


class UploadReview(BaseModel):
    confirmation: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')


class ParseApproval(BaseModel):
    attempt_token: str = Field(pattern=r'^[0-9a-f]{64}$')
    approved: bool = Field(strict=True)


def create_app(config_path=None, access_token=None, public_origin=None):
    config_path = Path(config_path or os.environ.get('COLLECTOR_CONFIG', HERE / 'config.json'))
    token = access_token if access_token is not None else os.environ.get('COLLECTOR_ACCESS_TOKEN', '')
    origin = (public_origin or os.environ.get('COLLECTOR_PUBLIC_ORIGIN', 'http://127.0.0.1:8765')).rstrip('/')
    parts = urlsplit(origin)
    if len(token) < 32:
        raise RuntimeError('COLLECTOR_ACCESS_TOKEN must be a random secret of at least 32 characters')
    if not parts.hostname or parts.path or parts.query or parts.fragment or parts.username or parts.password:
        raise RuntimeError('COLLECTOR_PUBLIC_ORIGIN must be an origin without a path')
    if parts.scheme != 'https' and not (parts.scheme == 'http' and parts.hostname in {'localhost', '127.0.0.1', '::1'}):
        raise RuntimeError('Public deployment requires HTTPS')
    _, root = load_config(config_path)
    initial = DraftStore(root)
    initial.close()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[parts.hostname])

    def signature(expiry):
        return hmac.new(token.encode(), ('collector-session:' + expiry).encode(), hashlib.sha256).hexdigest()

    def authenticated(request):
        value = request.cookies.get(COOKIE, '')
        expiry, _, signed = value.partition('.')
        return expiry.isdigit() and int(expiry) > time.time() and hmac.compare_digest(signed, signature(expiry))

    @app.middleware('http')
    async def guard(request, call_next):
        path = request.url.path
        if path.startswith('/api/'):
            if request.method not in {'GET', 'HEAD'}:
                if request.headers.get('x-collector-request') != '1' or request.headers.get('origin', origin) != origin:
                    return JSONResponse({'detail': '请求来源不匹配，请从收藏助手页面操作'}, status_code=403)
                length = request.headers.get('content-length', '')
                if not length.isdigit() or int(length) > 65536:
                    return JSONResponse({'detail': '请求须提供有效长度，且不能超过 64 KB'}, status_code=413)
                body = await request.body()
                if len(body) > 65536:
                    return JSONResponse({'detail': '提交内容太长'}, status_code=413)
            if path != '/api/login' and not authenticated(request):
                return JSONResponse({'detail': '请先输入访问口令'}, status_code=401)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @contextmanager
    def store():
        current = DraftStore(root)
        try:
            yield current
        finally:
            current.close()

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(DraftConflict)
    async def draft_conflict(request, exc):
        return JSONResponse({'detail': str(exc), 'code': 'draft_conflict', 'current': exc.current}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({'detail': '收藏不存在'}, status_code=404)

    @app.post('/api/login')
    def login(body: Login):
        if not hmac.compare_digest(body.token.encode(), token.encode()):
            raise HTTPException(401, '访问口令不正确')
        expiry = str(int(time.time()) + 30 * 86400)
        response = JSONResponse({'ok': True})
        response.set_cookie(COOKIE, expiry + '.' + signature(expiry), max_age=30 * 86400,
                            httponly=True, secure=parts.scheme == 'https', samesite='strict')
        return response

    @app.post('/api/logout')
    def logout():
        response = JSONResponse({'ok': True})
        response.delete_cookie(COOKIE)
        return response

    @app.get('/api/status')
    def status():
        config, _ = load_config(config_path)
        return dict(categories=list(config.get('categories', {})), **readiness(config, root))

    @app.get('/api/drafts')
    def drafts(offset: int = 0):
        if offset < 0:
            raise ValueError('页码无效')
        with store() as data:
            rows = data.all(51, offset)
            return {'items': rows[:50], 'has_more': len(rows) > 50}

    @app.post('/api/preview')
    def preview(body: Note):
        # Read-only: the same parser and filename function used by the queue.
        config, _ = load_config(config_path)
        comment, category = parse_note(body.note, config.get('categories', {}))
        filename = video_name(comment, '.mp4')
        return {'category': category, 'filename': filename,
                'adjusted': filename != comment + '.mp4'}

    @app.post('/api/drafts')
    def create(body: Share):
        with store() as data:
            return data.create(str(body.id), body.text)

    @app.get('/api/drafts/{draft_id}')
    def get_draft(draft_id: UUID):
        with store() as data:
            return data.get(str(draft_id))

    @app.patch('/api/drafts/{draft_id}')
    def save_note(draft_id: UUID, body: DraftNote):
        with store() as data:
            return data.save_note(str(draft_id), body.note, body.version)

    @app.post('/api/drafts/{draft_id}/submit')
    def submit(draft_id: UUID, body: DraftNote):
        config, _ = load_config(config_path)
        with store() as data:
            draft = data.get(str(draft_id))
            if draft['task_id']:
                # A response lost after commit must remain safely repeatable, even
                # if the worker or downloader is temporarily unavailable now.
                return data.submit(str(draft_id), body.note, config.get('categories', {}), body.version)
            try:
                saved = data.save_note(str(draft_id), body.note, body.version)
            except ValueError:
                # Another submit can finish between the initial read and draft save.
                if data.get(str(draft_id))['task_id']:
                    return data.submit(str(draft_id), body.note, config.get('categories', {}), body.version)
                raise
            def saved_error(message, status=409):
                return JSONResponse({'detail': message, 'code': 'note_saved', 'current': saved}, status_code=status)
            try:
                parse_note(body.note, config.get('categories', {}))
            except ValueError as exc:
                return saved_error(str(exc), 400)
            ready = readiness(config, root)
            if not ready['platforms'][draft['platform']]:
                problem = ready.get('platform_problems', {}).get(draft['platform']) or '这个平台的下载服务尚未接通'
                return saved_error('点评已保存；' + problem + '，暂未开始收藏')
            if not ready['quark_ready']:
                return saved_error('点评已保存；夸克上传服务尚未就绪，暂未开始收藏')
            if not ready['worker_online']:
                return saved_error('点评已保存；后台暂未运行，请稍后再试')
            return data.submit(str(draft_id), body.note, config.get('categories', {}), saved['version'])

    @app.post('/api/drafts/{draft_id}/cancel')
    def cancel(draft_id: UUID):
        with store() as data:
            try:
                return data.cancel(str(draft_id))
            except ValueError as exc:
                return JSONResponse({'detail': str(exc), 'code': 'cannot_cancel',
                                     'current': data.get(str(draft_id))}, status_code=409)

    @app.post('/api/drafts/{draft_id}/revise')
    def revise(draft_id: UUID):
        with store() as data:
            try:
                return data.revise(str(draft_id))
            except ValueError as exc:
                return JSONResponse({'detail':str(exc), 'code':'cannot_revise',
                                     'current':data.get(str(draft_id))}, status_code=409)

    @app.post('/api/drafts/{draft_id}/review')
    def review(draft_id: UUID, body: UploadReview):
        from web_recovery import review_upload
        config, _ = load_config(config_path)
        with store() as data:
            draft = data.get(str(draft_id))
            if not draft['task_id']:
                raise ValueError('这条视频尚未提交收藏，无需核对上传')
            try:
                extra = review_upload(data.queue, draft['task_id'], config, body.confirmation)
            except RuntimeError:
                raise HTTPException(409, '这条任务正在核对，请等待结果后刷新；不会重复上传。') from None
            return {**data.get(str(draft_id)), **extra}

    @app.post('/api/drafts/{draft_id}/retry')
    def retry(draft_id: UUID):
        config, _ = load_config(config_path)
        with store() as data:
            draft = data.get(str(draft_id))
            ready = readiness(config, root)
            if not ready['worker_online'] or not ready['quark_ready'] or not ready['platforms'][draft['platform']]:
                raise HTTPException(409, '处理服务尚未就绪，请稍后再试')
            if not draft['task_id']:
                raise ValueError('请先填写点评并提交')
            data.queue.retry(draft['task_id'])
            return data.get(str(draft_id))

    @app.post('/api/drafts/{draft_id}/reparse')
    def reparse(draft_id: UUID, body: ParseApproval):
        from parse_recovery import request_one_parse
        with store() as data:
            draft = data.get(str(draft_id))
            if draft['platform'] != 'wechat' or not draft['task_id']:
                raise ValueError('当前任务不需要重新解析视频号')
            request_one_parse(data.queue, draft['task_id'], body.attempt_token, body.approved)
            return data.get(str(draft_id))

    @app.get('/healthz')
    def health():
        return {'ok': True}

    @app.get('/')
    def index():
        return FileResponse(HERE / 'web' / 'index.html')

    @app.get('/share')
    def share():
        # GET only opens the form; authenticated JS explicitly creates the draft.
        return FileResponse(HERE / 'web' / 'index.html')

    @app.get('/sw.js')
    def service_worker():
        return FileResponse(HERE / 'web' / 'sw.js', media_type='application/javascript')

    app.mount('/assets', StaticFiles(directory=HERE / 'web'), name='assets')
    return app


def app():
    """Uvicorn factory: uvicorn web_app:app --factory."""
    return create_app()
