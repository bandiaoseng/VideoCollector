"""F2 parses public Douyin shares; yt-dlp transfers media; ffprobe verifies it.

F2 source pin: f6be8c0ffba9a127075bbeafe4838716650b6325. No browser-cookie
extraction, login, public parsing service, WeChat retrieval, or Quark operations.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urlsplit

from share_input import parse_share

F2_COMMIT = 'f6be8c0ffba9a127075bbeafe4838716650b6325'
MEDIA_DOMAINS = ('douyinvod.com', 'zjcdn.com', 'bytecdn.cn', 'byteimg.com', 'douyin.com')


class DownloadError(ValueError):
    pass


def media_allowed(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        return (parsed.scheme == 'https' and not parsed.username and not parsed.password
                and parsed.port in (None, 443)
                and any(host == domain or host.endswith('.' + domain) for domain in MEDIA_DOMAINS))
    except ValueError:
        return False


def select_media(response, expected_id, max_bytes=1024**3):
    detail = response.get('aweme_detail') or {}
    if response.get('status_code') != 0 or str(detail.get('aweme_id')) != str(expected_id):
        raise DownloadError('抖音没有返回匹配的作品；链接可能失效或需要重新获取游客会话')
    if detail.get('images'):
        raise DownloadError('目前仅支持视频，暂不把图文作品当视频保存')
    video = detail.get('video') or {}
    address = video.get('play_addr') or {}
    urls = [url for url in address.get('url_list', []) if isinstance(url, str) and media_allowed(url)]
    size = address.get('data_size')
    duration = video.get('duration')
    if not urls or not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= max_bytes:
        raise DownloadError('未取得允许的视频地址或文件超过大小限制')
    if not isinstance(duration, (int, float)) or not 0 < duration < 24 * 3600 * 1000:
        raise DownloadError('视频时长无效，未开始下载')
    return {'content_id': str(expected_id), 'urls': urls, 'bytes': size,
            'duration_seconds': duration / 1000, 'width': address.get('width'),
            'height': address.get('height'), 'resource_id': address.get('uri', '')}


async def resolve(source, max_bytes=1024**3):
    share = parse_share(source)
    if share['platform'] != 'douyin':
        raise DownloadError('此下载器仅处理抖音视频')
    # Optional imports keep core/offline unit tests independent from the downloader venv.
    from f2.apps.douyin.crawler import DouyinCrawler
    from f2.apps.douyin.model import PostDetail
    from f2.apps.douyin.utils import AwemeIdFetcher, ClientConfManager, TokenManager
    try:
        content_id = await AwemeIdFetcher.get_aweme_id(share['url'], proxies={'http://': None, 'https://': None})
        if not re.fullmatch(r'\d{10,25}', str(content_id)):
            raise DownloadError('分享链接没有对应到一个有效视频编号')
        # These are new anonymous visitor cookies from F2's documented token method.
        cookie = TokenManager.ensure_secsdk_uid('ttwid=' + TokenManager.gen_ttwid())
        kwargs = {'headers': ClientConfManager.headers(), 'cookie': cookie, 'timeout': 15,
                  'max_retries': 1, 'max_connections': 1, 'proxies': {'http://': None, 'https://': None}}
        async with DouyinCrawler(kwargs) as crawler:
            response = await crawler.fetch_post_detail(PostDetail(aweme_id=str(content_id)))
        return select_media(response, content_id, max_bytes)
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError('抖音解析暂未成功，请稍后重试；未下载或上传。类型：' + type(exc).__name__) from None


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_file(path, media, ffprobe='ffprobe'):
    if not path.is_file() or path.stat().st_size != media['bytes']:
        raise DownloadError('视频字节数不完整，未提交上传')
    with path.open('rb') as stream:
        if b'ftyp' not in stream.read(32):
            raise DownloadError('下载结果不是 MP4 视频')
    try:
        check = subprocess.run([ffprobe, '-v', 'error', '-show_entries',
            'format=duration,size:stream=codec_type,codec_name,width,height', '-of', 'json', str(path)],
            capture_output=True, text=True, encoding='utf-8', timeout=60, check=True)
        result = json.loads(check.stdout)
        duration = float(result['format']['duration'])
        videos = [s for s in result.get('streams', []) if s.get('codec_type') == 'video']
        if not videos or abs(duration - media['duration_seconds']) > 5:
            raise ValueError('duration or video stream mismatch')
        video = videos[0]
        if media.get('width') and media.get('height') and (video.get('width'), video.get('height')) != (media['width'], media['height']):
            raise ValueError('resolution mismatch')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise DownloadError('视频完整性检查失败；请检查 ffprobe 或重新下载') from None
    return result


def download_media(media, output, *, ffprobe='ffprobe', aria2=None):
    from yt_dlp import YoutubeDL
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = output.with_name(output.stem + '.stage.mp4')
    identity_path = output.with_suffix('.transfer.json')
    receipt = output.with_suffix('.receipt.json')
    identity = {k: media[k] for k in ('content_id', 'bytes', 'resource_id', 'width', 'height')}
    if output.exists():
        try:
            old = json.loads(receipt.read_text(encoding='utf-8'))
            if old['identity'] == identity and old['sha256'] == checksum(output):
                verify_file(output, media, ffprobe)
                return old
        except (OSError, ValueError, KeyError):
            pass
        raise DownloadError('目标文件已存在但无法核对来源，请先检查，未覆盖文件')
    try:
        previous = json.loads(identity_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        previous = None
    if previous != identity:
        # Only this adapter's exact staging names, never wildcard deletion.
        stage.unlink(missing_ok=True)
        Path(str(stage) + '.part').unlink(missing_ok=True)
        Path(str(stage) + '.part.aria2').unlink(missing_ok=True)
    identity_path.write_text(json.dumps(identity), encoding='utf-8')
    last_progress = [0.0]
    def progress(status):
        now = time.monotonic()
        if now - last_progress[0] > 20 or status['status'] == 'finished':
            print(json.dumps({'state': status['status'], 'bytes': status.get('downloaded_bytes'),
                              'total': status.get('total_bytes')}), flush=True)
            last_progress[0] = now
    class QuietLog:
        def debug(self, _): pass
        def warning(self, _): pass
        def error(self, _): pass
    verified = None
    transfer_options = {}
    if aria2:
        transfer_options = {'external_downloader': {'default': aria2},
                            'external_downloader_args': {'aria2c': ['-x4', '-s4', '-j1', '--continue=true',
                                '--connect-timeout=20', '--timeout=30', '--max-tries=2', '--retry-wait=3']}}
    for url in media['urls']:
        try:
            with YoutubeDL({'outtmpl': str(stage), 'max_filesize': media['bytes'], 'retries': 1,
                            'socket_timeout': 30, 'logger': QuietLog(), 'noprogress': True,
                            'progress_hooks': [progress], 'http_headers': {'Referer': 'https://www.douyin.com/'},
                            **transfer_options}) as ydl:
                if ydl.download([url]) != 0:
                    raise DownloadError('视频传输未成功')
            verified = verify_file(stage, media, ffprobe)
            break
        except Exception:
            # Do not accept truncated/HTML files as success. A valid partial can
            # resume against the next CDN mirror for the same resource identity.
            stage.unlink(missing_ok=True)
    if verified is None:
        raise DownloadError('视频下载或完整性检查未通过，未上传；可稍后重试')
    result = {'identity': identity, 'sha256': checksum(stage), 'media_probe': verified,
              'parsed_by': 'F2', 'f2_commit': F2_COMMIT, 'transferred_by': 'yt-dlp + aria2' if aria2 else 'yt-dlp',
              'checked_at_unix': time.time()}
    stage.replace(output)
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--max-bytes', type=int, default=1024**3)
    parser.add_argument('--ffprobe', default='ffprobe')
    parser.add_argument('--aria2', help='Optional aria2c path for four-connection HTTP transfer')
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        if not shutil.which(args.ffprobe):
            raise DownloadError('未找到 ffprobe，请先安装视频校验工具；未开始下载')
        if args.aria2 and not shutil.which(args.aria2):
            raise DownloadError('指定的 aria2 下载工具不存在，未开始下载')
        media = asyncio.run(asyncio.wait_for(resolve(args.source, args.max_bytes), timeout=90))
        print(json.dumps({'state': 'resolved', **{k:v for k,v in media.items() if k != 'urls'}}), flush=True)
        result = download_media(media, args.output, ffprobe=args.ffprobe, aria2=args.aria2)
        print(json.dumps({'state':'downloaded', 'bytes':media['bytes'], 'sha256':result['sha256']}), flush=True)
    except Exception as exc:
        print(json.dumps({'state':'failed', 'error':str(exc) if isinstance(exc, DownloadError) else type(exc).__name__}, ensure_ascii=False), flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
