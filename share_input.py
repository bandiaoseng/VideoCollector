"""Normalize phone share text without contacting external services."""
import re
from urllib.parse import urlsplit, urlunsplit


def parse_share(text: str) -> dict:
    candidates = re.findall(r'https?://[^\s<>"\u3000\[\]()\u3002\uff0c\uff1b\uff01\uff09]+', text)
    supported = {}
    for raw in candidates:
        url = raw.rstrip('.,;!?)\u3002\uff0c\uff1b\uff01\uff09]')
        parts = urlsplit(url)
        host = (parts.hostname or '').lower()
        if parts.username or parts.password or parts.port not in (None, 443):
            continue
        platform = None
        if host == 'weixin.qq.com' and parts.path.startswith('/sph/'):
            platform = 'wechat'
        elif host in {'v.douyin.com', 'www.douyin.com', 'www.iesdouyin.com'}:
            platform = 'douyin'
        if platform:
            normalized = urlunsplit(('https', host, parts.path, parts.query, ''))
            supported[normalized] = platform
    if not supported:
        raise ValueError('未找到支持的抖音或视频号分享链接')
    if len(supported) != 1:
        raise ValueError('一次请提交一个视频，避免点评与视频对应错误')
    url, platform = next(iter(supported.items()))
    return {'url': url, 'platform': platform}
