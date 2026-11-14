"""Zhiling Channels adapter. No request on import or configuration checks.

Only the unencrypted-video branch has a user-verified sample. Encrypted media
is rejected until independently validated. The CLI is run in the collector's
managed subprocess, bounding network reads and codec subprocesses on cancel.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import socket
import ssl
import http.client
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from share_input import parse_share

API_URL = 'https://api.17zhiling.com/api/video/parse-wechat-url'
MAX_RESPONSE = 2 * 1024 * 1024


class ZhilingError(ValueError):
    pass


class ConnectionFailure(ZhilingError):
    def __init__(self, exc, stage):
        reason = getattr(exc, 'reason', exc)
        if isinstance(reason, ssl.SSLError):
            kind = 'tls'
            text = ('加密连接被中途关闭' if isinstance(reason, ssl.SSLEOFError)
                    or getattr(reason, 'errno', None) == ssl.SSL_ERROR_EOF else '安全连接建立失败')
        elif isinstance(reason, socket.gaierror):
            kind, text = 'dns', '无法找到服务地址'
        elif isinstance(reason, (TimeoutError, socket.timeout)):
            kind, text = 'timeout', '连接或等待回复超时'
        else:
            kind, text = 'connection', '网络连接中断或无法建立'
        service = {'parse': '智凌解析', 'preflight': '智凌连接检查'}.get(stage, '视频下载')
        tail = {'parse': '没有拿到解析结果，未自动重复请求。',
                'preflight': '本次没有调用解析接口；请先恢复网络连接，再继续此任务。'}.get(
                    stage, '解析结果已保留；继续任务不会重新解析。')
        super().__init__(f'{service}：{text}。{tail}')
        number = getattr(reason, 'errno', None)
        self.details = {'stage': stage, 'kind': kind,
                        'errno': number if type(number) is int else None}
        if isinstance(reason, ssl.SSLError):
            # Store fixed classifications, never raw errors containing request URLs.
            self.details['tls_reason'] = (
                'unexpected_eof' if isinstance(reason, ssl.SSLEOFError)
                or number == ssl.SSL_ERROR_EOF else
                'certificate' if isinstance(reason, ssl.SSLCertVerificationError) else 'other')


def positive_integer(config, name, default):
    value = config.get(name, default)
    if type(value) is not int or value <= 0:
        raise ZhilingError('智凌配置中的大小或超时限制必须为正整数')
    return value


def read_key(config):
    env_name = config.get('api_key_env', 'ZHILING_API_KEY')
    if not isinstance(env_name, str) or not env_name:
        raise ZhilingError('智凌密钥配置不正确')
    key = '' if config.get('api_key_file') else os.environ.get(env_name, '')
    if config.get('api_key_file'):
        path = Path(config['api_key_file'])
        if not path.is_absolute():
            raise ZhilingError('智凌密钥文件请配置为绝对路径')
        try:
            if path.stat().st_size > 4096:
                raise ZhilingError('智凌密钥文件格式不正确')
            key = path.read_text(encoding='utf-8-sig').strip()
        except OSError:
            raise ZhilingError('尚未填写可读取的智凌密钥文件') from None
    if not key or len(key) > 4096 or any(c.isspace() or ord(c) < 32 for c in key):
        raise ZhilingError('尚未填写有效的智凌 API 密钥')
    return key


def check_settings(config):
    if not isinstance(config, dict) or config.get('enabled') is not True:
        raise ZhilingError('智凌视频号下载尚未启用')
    read_key(config)
    for name in ('ffprobe', 'ffmpeg'):
        executable = config.get(name, name)
        if not isinstance(executable, str) or not shutil.which(executable):
            raise ZhilingError('视频校验工具未安装或路径无效')
    positive_integer(config, 'request_timeout', 30)
    positive_integer(config, 'validation_timeout', 180)
    positive_integer(config, 'max_bytes', 1024 ** 3)


def media_url(value):
    try:
        parts = urllib.parse.urlsplit(value)
        valid = (isinstance(value, str) and not any(c.isspace() for c in value)
                 and parts.scheme == 'https' and parts.hostname == 'finder.video.qq.com'
                 and parts.port in (None, 443) and not parts.username and not parts.password
                 and not parts.fragment and parts.path.endswith('/stodownload'))
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ZhilingError('解析未返回已支持的视频文件地址，未下载网页或未知来源')
    return value


def select_video(envelope):
    if not isinstance(envelope, dict) or type(envelope.get('code')) is not int or envelope['code'] != 200:
        # Upstream messages can contain URLs/credentials, so do not echo them.
        raise ZhilingError('智凌解析未成功，未开始下载；请在服务商调用记录中核对原因')
    data = envelope.get('data')
    if not isinstance(data, dict):
        raise ZhilingError('智凌返回的数据不完整')
    if data.get('encryption') is not False:
        raise ZhilingError('此视频加密或加密状态不明，当前适配器尚不支持，未上传')
    if data.get('picsList') or data.get('videosList'):
        raise ZhilingError('结果包含图集或多个视频，无法匹配本次点评')
    return {'url': media_url(data.get('url')), 'title': data.get('title') or ''}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ZhilingError('服务返回重定向，未转发密钥或视频签名')


class NoProbeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_parser_connection(config=None):
    """HEAD the public origin only. No key, video link, API call or redirects.

    Uses urllib's ordinary certificate verification and proxy handling, matching
    the parser transport. Success proves connectivity, not quota or parsing.
    """
    timeout = min(positive_integer(config or {}, 'request_timeout', 30), 10)
    origin = urllib.parse.urlsplit(API_URL)
    request = urllib.request.Request(
        urllib.parse.urlunsplit((origin.scheme, origin.netloc, '/', '', '')),
        method='HEAD', headers={'User-Agent': 'video-collector/0.1', 'Accept-Encoding': 'identity'})
    opener = urllib.request.build_opener(NoProbeRedirect())
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        # Even a HEAD-not-supported or redirect reply establishes TLS/HTTP.
        status = exc.code
        exc.close()
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise ConnectionFailure(exc, 'preflight') from None
    if not 200 <= status < 500:
        failure = ZhilingError(f'智凌连接检查：服务暂时异常（HTTP {status}）。本次没有调用解析接口，请稍后继续此任务。')
        failure.details = {'stage': 'preflight', 'kind': 'http', 'http_status': status}
        raise failure
    return {'host': origin.hostname, 'method': 'HEAD', 'http_status': status,
            'seconds': round(time.monotonic() - started, 3), 'parser_called': False}


class Client:
    def __init__(self, config):
        self.key = read_key(config)
        self.timeout = positive_integer(config, 'request_timeout', 30)
        self.opener = urllib.request.build_opener(NoRedirect())

    def check_connection(self):
        return check_parser_connection({'request_timeout': self.timeout})

    def open(self, url, stage='parse'):
        try:
            return self.opener.open(urllib.request.Request(url, headers={
                'User-Agent': 'video-collector/0.1', 'Accept-Encoding': 'identity'}), timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise ZhilingError(f'服务请求未成功（HTTP {status}），未自动重试') from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise ConnectionFailure(exc, stage) from None

    def parse(self, source):
        url = API_URL + '?' + urllib.parse.urlencode({'key': self.key, 'url': source})
        try:
            with self.open(url, 'parse') as response:
                raw = response.read(MAX_RESPONSE + 1)
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise ConnectionFailure(exc, 'parse') from None
        if len(raw) > MAX_RESPONSE:
            raise ZhilingError('解析响应过大，未处理')
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise ZhilingError('解析服务未返回有效数据') from None

    def media(self, url):
        return self.open(media_url(url), 'media')


def private_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def checksum(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def verify_file(path, config):
    with path.open('rb') as stream:
        if stream.read(12)[4:8] != b'ftyp':
            raise ZhilingError('下载结果不是 MP4 文件，未上传')
    try:
        probe = subprocess.run([config.get('ffprobe', 'ffprobe'), '-v', 'error',
            '-show_entries', 'format=duration:stream=codec_type,width,height', '-of', 'json', str(path)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=True)
        report = json.loads(probe.stdout)
        duration = float(report['format']['duration'])
        if not math.isfinite(duration) or duration <= 0 or not any(
                s.get('codec_type') == 'video' and s.get('width', 0) > 0 and s.get('height', 0) > 0
                for s in report['streams']):
            raise ValueError('Invalid video')
        subprocess.run([config.get('ffmpeg', 'ffmpeg'), '-hide_banner', '-nostdin', '-v', 'error',
            '-xerror', '-err_detect', 'explode', '-i', str(path), '-map', '0:v:0', '-map', '0:a:0?',
            '-f', 'null', '-'], stdin=subprocess.DEVNULL, capture_output=True,
            timeout=positive_integer(config, 'validation_timeout', 180), check=True)
        return {'duration': duration, 'audio_present': any(s.get('codec_type') == 'audio' for s in report['streams'])}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        raise ZhilingError('下载文件未通过完整解码检查，未上传') from None


def download(config, source, output, *, client=None):
    check_settings(config)
    share = parse_share(source)
    if share['platform'] != 'wechat':
        raise ZhilingError('智凌适配器只处理视频号分享链接')
    source = share['url']
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    client = client or Client(config)
    identity = hashlib.sha256(source.encode()).hexdigest()
    cache_path = output.with_suffix('.zhiling-private.json')
    approval_path = output.with_suffix('.zhiling-approval.json')
    parse_needed = not cache_path.exists()
    previous = None
    if cache_path.exists():
        try:
            raw_cache = cache_path.read_bytes()
            cache = json.loads(raw_cache)
        except (OSError, ValueError):
            raise ZhilingError('解析记录损坏，未重复调用接口') from None
        if not isinstance(cache, dict) or cache.get('source_hash') != identity:
            raise ZhilingError('解析记录与来源不一致，未调用接口')
        if cache.get('state') != 'received':
            try:
                approval = json.loads(approval_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                approval = None
            if cache.get('state') != 'request_started' or approval != {
                    'version': 1, 'task_id': output.parent.name, 'source_hash': identity, 'attempt_token': hashlib.sha256(raw_cache).hexdigest()}:
                raise ZhilingError('上次解析结果不明，未重复消耗额度；请先核对服务商调用记录')
            parse_needed, previous = True, cache
        else:
            envelope = cache.get('response')
    elif approval_path.exists():
        raise ZhilingError('原解析记录缺失，不能使用旧批准重新请求；未调用接口')
    if parse_needed:
        # A failed unbilled check must not be recorded as a billed parse attempt.
        client.check_connection()
        # Write before requesting: a timeout/crash must not cause a second billed request.
        if previous is not None:
            private_json(cache_path.with_name('download.zhiling-private.attempt-' + uuid.uuid4().hex + '.json'), previous)
        attempt_id = uuid.uuid4().hex
        # Replacing the old barrier consumes its approval BEFORE the request.
        # A crash here leaves a new, unapproved barrier, never an empty gap.
        private_json(cache_path, {'source_hash': identity, 'state': 'request_started', 'attempt_id': attempt_id})
        envelope = client.parse(source)
        # Cache only the media fields needed for resuming, never upstream messages/key/IP.
        data = envelope.get('data') if isinstance(envelope, dict) else None
        trimmed = {'code': envelope.get('code') if isinstance(envelope, dict) else None,
                   'data': {k: data.get(k) for k in ('url', 'title', 'encryption', 'picsList', 'videosList')}
                   if isinstance(data, dict) else None}
        private_json(cache_path, {'source_hash': identity, 'state': 'received', 'attempt_id': attempt_id, 'response': trimmed})
    media = select_video(envelope)
    partial = output.with_suffix('.zhiling-part')
    max_bytes = positive_integer(config, 'max_bytes', 1024 ** 3)
    try:
        with client.media(media['url']) as response, partial.open('wb') as stream:
            if response.status != 200:
                raise ZhilingError('视频下载未返回完整文件响应')
            kind = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
            if kind.startswith('text/') or kind in {'application/json', 'application/xhtml+xml'}:
                raise ZhilingError('下载地址返回网页或错误信息，未上传')
            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise ZhilingError('视频传输编码未受支持')
            length = response.headers.get('Content-Length')
            try:
                expected = int(length) if length is not None else None
            except (TypeError, ValueError):
                raise ZhilingError('视频大小响应无效') from None
            if expected is not None and (expected <= 0 or expected > max_bytes):
                raise ZhilingError('视频为空或超出配置的大小限制')
            total = 0
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > max_bytes or (expected is not None and total > expected):
                    raise ZhilingError('视频传输超出预期大小，未上传')
                stream.write(block)
        if total == 0 or (expected is not None and total != expected):
            raise ZhilingError('视频没有下载完整，未上传')
        details = verify_file(partial, config)
        receipt = {'provider': 'zhiling', 'source': source, 'bytes': total,
                   'sha256': checksum(partial), **details}
        private_json(output.with_suffix('.zhiling.json'), receipt)
        partial.replace(output)
        return receipt
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        # A local file error must not be mislabeled as an Internet failure.
        if isinstance(exc, (PermissionError, FileNotFoundError)) or getattr(exc, 'errno', None) in (28, 112):
            raise ZhilingError('本机无法保存视频，请检查磁盘空间和文件权限；解析结果已保留。') from None
        raise ConnectionFailure(exc, 'media') from None
    finally:
        partial.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='视频号下载适配器；执行下载会消耗服务商额度')
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--settings-json', required=True, help='不含密钥的本机配置')
    args = parser.parse_args(argv)
    try:
        result = download(json.loads(args.settings_json), args.source, args.output)
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=True))
        return 0
    except ZhilingError as exc:
        try:
            private_json(args.output.with_suffix('.zhiling-error.json'),
                         {'error': str(exc), 'details': getattr(exc, 'details', None)})
        except OSError:
            pass
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=True))
        return 1
    except Exception:
        # No traceback, raw URL, upstream response or secret in worker logs.
        print(json.dumps({'ok': False, 'error': '视频号处理失败，未上传；未自动重复解析'}, ensure_ascii=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
