"""Adapter for the pinned DTK v5.1.7 API; DTK performs parsing and CDN downloads.

Contract: upstream/dtk/documents/zh/{11-api,08-downloads-and-library}.md.
The public demo is deliberately not a production backend.
"""
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request


class DtkError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DtkError('下载服务发生重定向，请检查配置地址；未转发凭据')


class Client:
    def __init__(self, config):
        self.base = str(config.get('base_url', '')).rstrip('/')
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.hostname == 'demo.douyin.wtf':
            raise DtkError('公开演示站仅用于兼容性测试，请配置自己的下载服务')
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme not in {'https', 'http'}
            or (parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1', 'localhost', '::1'})):
            raise DtkError('请配置 HTTPS 下载服务地址，或本机回环地址')
        self.key = os.environ.get(config.get('api_key_env', 'DTK_API_KEY'), '')
        if not self.key:
            raise DtkError('尚未配置下载服务 API Key')
        self.opener = urllib.request.build_opener(NoRedirect())

    def open(self, route, body=None):
        req = urllib.request.Request(self.base + route,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json',
                     'User-Agent': 'video-collector/0.1'})
        try:
            return self.opener.open(req, timeout=35)
        except urllib.error.HTTPError as exc:
            with exc:
                try:
                    code = json.loads(exc.read(65536)).get('error', {}).get('code')
                except (ValueError, AttributeError):
                    code = None
            raise DtkError(f'下载服务请求失败（HTTP {exc.code}，{code or "UNKNOWN"}）') from None
        except urllib.error.URLError:
            raise DtkError('无法连接下载服务，已保留任务编号以便接续') from None

    def json(self, route, body=None):
        with self.open(route, body) as response:
            envelope = json.loads(response.read(8 * 1024 * 1024))
        if envelope.get('success') is not True:
            raise DtkError(str((envelope.get('error') or {}).get('code', 'INVALID_RESPONSE')))
        return envelope['data']


def wait_for(read, *, timeout=900, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    while True:
        data = read()
        state = data.get('state')
        if state == 'done':
            return data
        if state in {'failed', 'cancelled', 'partial'}:
            raise DtkError(f'下载任务未完整成功：{state}')
        if state not in {'queued', 'running'}:
            raise DtkError('下载服务返回未知任务状态')
        if clock() >= deadline:
            raise DtkError('等待超时，远端任务编号已保留；重试将继续查询原任务')
        sleep(3)


def select_video(record):
    if record.get('state') != 'done' or record.get('on_disk') is not True:
        raise DtkError('远端视频尚未完整保存或已经被清理')
    files = [item for item in record.get('files', [])
             if item.get('kind') == 'video' and item.get('state') == 'done']
    if len(files) != 1:
        raise DtkError('下载结果不是单个视频，无法自动匹配本次点评')
    entry = files[0]
    name = entry.get('name', '')
    if not name or '/' in name or '\\' in name or not name.lower().endswith('.mp4'):
        raise DtkError('下载结果不是预期的 MP4 文件')
    if not isinstance(entry.get('bytes'), int) or entry['bytes'] <= 0:
        raise DtkError('远端没有返回有效视频大小')
    return entry


def download(config, source, output, *, client=None, cancel_check=None):
    def checkpoint():
        if cancel_check and cancel_check():
            from process_runner import ProcessCancelled
            raise ProcessCancelled('已请求取消')
    checkpoint()
    client = client or Client(config)
    def checked_json(route, body=None):
        checkpoint()
        return client.json(route, body)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt_path = output.with_suffix('.dtk.json')
    identity = hashlib.sha256((client.base + '\n' + source).encode()).hexdigest()
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        if receipt.get('identity') != identity:
            raise DtkError('本地接续记录与视频或下载服务不匹配')
    else:
        receipt = {'identity': identity}

    def save():
        temporary = receipt_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(receipt), encoding='utf-8')
        temporary.replace(receipt_path)

    if 'parse_task' not in receipt:
        task = checked_json('/api/v1/parse', {'url': source})
        receipt['parse_task'] = task['task_id']
        save()
    if 'content_id' not in receipt:
        task = wait_for(lambda: checked_json('/api/v1/tasks/' + urllib.parse.quote(receipt['parse_task'], safe='')),
                        timeout=config.get('timeout', 900))
        content = task.get('data') or {}
        if content.get('platform') != 'douyin' or content.get('kind') != 'video' or not content.get('content_id'):
            raise DtkError('解析结果不是抖音视频')
        receipt['content_id'] = content['content_id']
        save()
    if 'download_id' not in receipt:
        result = checked_json('/api/v1/downloads', {'platform': 'douyin', 'content_id': receipt['content_id'], 'skip_existing': True})
        receipt['download_id'] = result['download_id']
        save()
    route = '/api/v1/downloads/' + urllib.parse.quote(receipt['download_id'], safe='')
    record = wait_for(lambda: checked_json(route), timeout=config.get('timeout', 900))
    entry = select_video(record)
    max_bytes = config.get('max_bytes', 1024 * 1024 * 1024)
    if entry['bytes'] > max_bytes:
        raise DtkError('视频超出配置的下载大小限制')
    partial = output.with_suffix('.transfer-part')
    try:
        total = 0
        with client.open(route + '/files/' + urllib.parse.quote(entry['name'], safe='')) as response, partial.open('wb') as stream:
            while chunk := response.read(1024 * 1024):
                checkpoint()
                total += len(chunk)
                if total > entry['bytes'] or total > max_bytes:
                    raise DtkError('视频传输大小与远端记录不符')
                stream.write(chunk)
        if total != entry['bytes']:
            raise DtkError('视频传输不完整，尚未上传夸克')
        with partial.open('rb') as stream:
            if stream.read(12)[4:8] != b'ftyp':
                raise DtkError('文件不是预期的 MP4，尚未上传夸克')
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)
    return output
