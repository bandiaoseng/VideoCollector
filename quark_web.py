"""Independent Quark web-session client. Never uses the Agent SDK or its tokens.

Protocol references: OpenListTeam/OpenList drivers/quark_uc; imeiming/quark-drive
QR login endpoints. Implemented locally; no upstream source is bundled.
"""
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import time
import tempfile
import uuid
from email.utils import formatdate
from urllib.parse import urlencode, urlsplit
from xml.sax.saxutils import escape

import httpx

API = 'https://drive-pc.quark.cn/1/clouddrive'
PAN = 'https://pan.quark.cn'
UOP = 'https://uop.quark.cn/cas/ajax/'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36'
OSS_UA = 'aliyun-sdk-js/6.6.1'


class WebError(ValueError):
    pass


class SessionBusy(WebError):
    pass


def save_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the temporary basename short: desktop receipt directories can be
    # close to Windows' path length limit even when the final file is valid.
    fd, name = tempfile.mkstemp(prefix='.w-', suffix='.tmp', dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        # Windows can briefly refuse a replace while another atomic rename is
        # finishing. Retry only this local file operation, never a service call.
        for attempt in range(6):
            try:
                tmp.replace(path)
                break
            except PermissionError as exc:
                if os.name != 'nt' or exc.winerror not in {5, 32, 33} or attempt == 5:
                    raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def session_access(path):
    """Serialize readers that refresh cookies and explicit login replacement."""
    from collector import WorkerLock
    path = Path(path).resolve()
    home = path.parent / ('.' + path.name + '.access')
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = WorkerLock(home)
    try:
        lock.__enter__()
    except RuntimeError:
        raise SessionBusy('夸克登录记录正被另一任务或扫码窗口使用，请等它结束后再继续原任务；无需重新下载。') from None
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def close_client(client):
    """Also accept older injected adapters which expose only http.close()."""
    if client is not None:
        closer = getattr(client, 'close', None)
        (closer if callable(closer) else client.http.close)()


def http_client():
    return httpx.Client(timeout=40, follow_redirects=False, headers={
        'User-Agent': UA, 'Referer': PAN + '/', 'Origin': PAN,
        'Accept': 'application/json, text/plain, */*'})


class WebClient:
    def __init__(self, session_path, *, client=None, load_session=True):
        self.session_path = Path(session_path)
        self._access = session_access(self.session_path)
        self._access.__enter__()
        self.http = None
        try:
            self.http = client or http_client()
            if load_session and self.session_path.exists():
                data = json.loads(self.session_path.read_text(encoding='utf-8'))
                for c in data['cookies']:
                    if c['domain'].lstrip('.') in {'quark.cn', 'pan.quark.cn', 'drive-pc.quark.cn', 'uop.quark.cn'}:
                        self.http.cookies.set(c['name'], c['value'], domain=c['domain'], path=c.get('path', '/'))
        except BaseException as exc:
            try:
                self.close()
            except Exception:
                pass
            if isinstance(exc, (OSError, ValueError, KeyError, TypeError, AttributeError)):
                raise WebError('本机夸克登录记录不可用，请重新扫码登录。') from None
            raise

    def close(self):
        access, self._access = self._access, None
        if access is not None:
            try:
                if self.http is not None:
                    self.http.close()
            finally:
                access.__exit__(None, None, None)

    def save(self):
        if self._access is None:
            raise WebError('连接已关闭，请继续原任务重新连接夸克。')
        cookies = [{'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path}
                   for c in self.http.cookies.jar if c.domain.lstrip('.') in
                   {'quark.cn', 'pan.quark.cn', 'drive-pc.quark.cn', 'uop.quark.cn'}]
        save_private(self.session_path, {'cookies': cookies})

    def request_json(self, method, url, **kwargs):
        if self._access is None:
            raise WebError('连接已关闭，请继续原任务重新连接夸克。')
        try:
            r = self.http.request(method, url, **kwargs)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (httpx.HTTPError, ValueError):
            raise WebError('连接夸克失败或返回异常；未自动重复请求。') from None

    def api(self, path, body=None, params=None):
        result = self.request_json('GET' if body is None else 'POST', API + path,
            params={'pr': 'ucpro', 'fr': 'pc', **(params or {})},
            **({'json': body} if body is not None else {}))
        if result.get('status') != 200 or result.get('code', 0) != 0:
            code = result.get('code')
            raise WebError(f'夸克未接受请求（错误码 {code}）；请检查登录状态或账户容量。')
        if self.session_path.exists():
            self.save()
        return result

    def list_all(self, parent):
        files, seen = [], set()
        for page in range(1, 10001):
            result = self.api('/file/sort', params={'pdir_fid': parent, '_page': page,
                '_size': 100, '_fetch_total': 1, 'fetch_all_file': 1,
                'fetch_risk_file_name': 1, '_sort': 'file_name:asc'})
            batch = result.get('data', {}).get('list')
            total = result.get('metadata', {}).get('_total')
            if not isinstance(batch, list) or type(total) is not int or total < 0:
                raise WebError('夸克目录清单不完整，未开始上传。')
            for row in batch:
                if not isinstance(row, dict) or not row.get('fid') or not isinstance(row.get('file_name'), str) or row['fid'] in seen:
                    raise WebError('夸克目录分页发生变化，未开始上传。')
                seen.add(row['fid']); files.append(row)
            if len(files) == total:
                return files
            if not batch or len(files) > total:
                raise WebError('夸克目录数量发生变化，请稍后重试。')
        raise WebError('目录过大，尚未完成核对。')

    def resolve(self, parts):
        if not isinstance(parts, list) or not parts or not all(isinstance(p, str) and p and '/' not in p for p in parts):
            raise WebError('没有配置明确的夸克目标目录。')
        parent = '0'
        for part in parts:
            rows = [r for r in self.list_all(parent) if r['file_name'] == part and r.get('file') is False]
            if len(rows) != 1:
                raise WebError(f'找不到唯一的“{part}”文件夹；请确认登录了正确的夸克账号。')
            parent = rows[0]['fid']
        return parent

    def qr_start(self):
        data = self.request_json('GET', UOP + 'getTokenForQrcodeLogin', params={
            'client_id': '532', 'v': '1.2', 'request_id': str(uuid.uuid4())})
        if data.get('status') != 2000000:
            raise WebError('夸克未返回登录二维码，请稍后重试。')
        token = data['data']['members']['token']
        url = 'https://su.quark.cn/4_eMHBJ?' + urlencode({'token': token, 'client_id': '532',
            'ssb': 'weblogin', 'uc_param_str': '',
            'uc_biz_str': 'S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0'})
        return token, url

    def qr_poll(self, token):
        data = self.request_json('GET', UOP + 'getServiceTicketByQrcodeToken', params={
            'client_id': '532', 'v': '1.2', 'token': token, 'request_id': str(uuid.uuid4())})
        ticket = (data.get('data') or {}).get('members', {}).get('service_ticket')
        if not ticket:
            return False
        self.request_json('GET', PAN + '/account/info', params={'st': ticket, 'lw': 'scan'})
        self.list_all('0')  # Verify drive access before saving a session.
        self.save()
        return True

    def upload(self, file, parent, progress=lambda text: None):
        file = Path(file)
        hashes = {k: hashlib.new(k) for k in ('md5', 'sha1', 'sha256')}
        with file.open('rb') as f:
            for block in iter(lambda: f.read(1024 * 1024), b''):
                for h in hashes.values(): h.update(block)
        now = int(time.time() * 1000)
        pre = self.api('/file/upload/pre', body={'ccp_hash_update': True, 'dir_name': '',
            'file_name': file.name, 'format_type': 'video/mp4', 'l_created_at': now,
            'l_updated_at': now, 'pdir_fid': parent, 'size': file.stat().st_size})
        info = pre['data']
        task_id = info['task_id']
        hashed = self.api('/file/update/hash', body={'task_id': task_id,
            'md5': hashes['md5'].hexdigest(), 'sha1': hashes['sha1'].hexdigest()})
        if hashed.get('data', {}).get('finish') is not True:
            self.multipart(file, info, pre.get('metadata', {}).get('part_size'), progress)
            self.api('/file/upload/finish', body={'obj_key': info['obj_key'], 'task_id': task_id})
        return {name: h.hexdigest() for name, h in hashes.items()}

    def multipart(self, file, info, part_size, progress):
        if type(part_size) is not int or not 1024 <= part_size <= 64 * 1024 * 1024:
            raise WebError('夸克分片参数异常，停止上传并保留任务。')
        host = urlsplit(info['upload_url']).hostname
        bucket, key, upload_id = info['bucket'], info['obj_key'], info['upload_id']
        if (not host or not (host == 'pds.quark.cn' or host.endswith('.pds.quark.cn')
                            or host.endswith('.aliyuncs.com')) or not re.fullmatch(r'[a-zA-Z0-9-]+', bucket)
                or not isinstance(key, str) or any(c in key for c in ['?', '#', '\r', '\n'])):
            raise WebError('上传存储地址不符合预期，停止上传。')
        base = f'https://{bucket}.{host}/{key}'

        def send(method, content, mime, suffix, extra=None):
            date = formatdate(usegmt=True)
            headers = {'Content-Type': mime, 'x-oss-date': date, 'x-oss-user-agent': OSS_UA,
                       'Referer': PAN + '/', **(extra or {})}
            oss = ''.join(k + ':' + headers[k] + '\n' for k in sorted(headers) if k.startswith('x-oss-'))
            sign = '\n'.join([method, headers.get('Content-MD5', ''), mime, date]) + '\n' + oss + f'/{bucket}/{key}?{suffix}'
            auth = self.api('/file/upload/auth', body={'task_id': info['task_id'],
                'auth_info': info['auth_info'], 'auth_meta': sign})['data']['auth_key']
            headers['Authorization'] = auth
            # Separate transport: account cookies never go to object storage.
            try:
                response = httpx.request(method, base + '?' + suffix, content=content,
                                         headers=headers, timeout=120, follow_redirects=False)
                if response.status_code != 200:
                    raise WebError('视频分片或提交未得到成功响应，结果需核对。')
                return response
            except httpx.HTTPError:
                raise WebError('上传连接中断，结果需核对；不会自动重复上传。') from None

        etags = []
        with file.open('rb') as stream:
            while block := stream.read(part_size):
                part = len(etags) + 1
                response = send('PUT', block, 'video/mp4', urlencode({'partNumber': part, 'uploadId': upload_id}))
                etag = response.headers.get('ETag')
                if not etag: raise WebError('上传缺少分片回执，停止并保留任务。')
                etags.append(etag)
                progress(f'正在上传：{min(95, int(stream.tell() / file.stat().st_size * 95))}%')
        xml = ('<?xml version="1.0" encoding="UTF-8"?><CompleteMultipartUpload>' +
               ''.join(f'<Part><PartNumber>{i}</PartNumber><ETag>{escape(etag)}</ETag></Part>'
                       for i, etag in enumerate(etags, 1)) + '</CompleteMultipartUpload>').encode()
        extra = {'Content-MD5': base64.b64encode(hashlib.md5(xml).digest()).decode(),
                 'x-oss-callback': base64.b64encode(json.dumps(info['callback'], separators=(',', ':')).encode()).decode()}
        send('POST', xml, 'application/xml', urlencode({'uploadId': upload_id}), extra)
