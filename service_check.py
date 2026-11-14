"""Local setup checks only: never fetch videos, log in, or upload files."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import time

from collector import QuarkUploader
from runtime_config import load_runtime_config


def worker_alive(root):
    try:
        stamp = json.loads((Path(root) / 'worker-heartbeat.json').read_text())['updated']
        return 0 <= time.time() - stamp < 35
    except (OSError, ValueError, KeyError, TypeError):
        return False


def command_problem(command, deep=False):
    if not command:
        return '尚未配置下载服务'
    if not isinstance(command, list) or not all(isinstance(arg, str) and arg for arg in command):
        return '下载命令配置格式不正确'
    if not shutil.which(command[0]):
        return '下载程序未安装或路径已失效'
    if not all(any(marker in arg for arg in command) for marker in ('{source}', '{output}')):
        return '下载命令缺少视频来源或输出位置'
    script = Path(command[1]) if len(command) > 1 and command[1].endswith('.py') else None
    if script is not None and not script.is_file():
        return '下载脚本不存在'
    if script is not None and script.name == 'douyin_f2.py':
        for flag, fallback, label in (('--ffprobe', 'ffprobe', '视频校验工具'), ('--aria2', None, '下载加速工具')):
            if flag in command:
                index = command.index(flag) + 1
                executable = command[index] if index < len(command) else None
                if not executable or executable.startswith('--'):
                    return label + '的配置不完整'
            else:
                executable = fallback
            if executable and not shutil.which(executable):
                return label + '未安装或路径已失效'
        if deep:
            try:
                # Only the pinned adapter's dependencies; no URL or credentials.
                result = subprocess.run([command[0], '-c',
                    'from f2.apps.douyin.crawler import DouyinCrawler; from yt_dlp import YoutubeDL'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                if result.returncode:
                    return '抖音下载依赖未完整安装'
            except (OSError, subprocess.SubprocessError):
                return '抖音下载依赖检查未通过'
    return None


def quark_problem(config):
    """Check local setup only: do not construct a network client or preflight it."""
    if 'quark_web' not in config:
        try:
            QuarkUploader(config).preflight()  # Legacy preflight only checks local tools.
            return None
        except Exception:
            return '夸克上传工具或授权配置尚未就绪'
    settings = config['quark_web']
    if not isinstance(settings, dict):
        return '独立夸克配置格式不正确'
    if importlib.util.find_spec('httpx') is None:
        return '独立夸克上传依赖尚未安装'
    categories, mapping = config.get('categories'), settings.get('category_paths')
    if (not isinstance(categories, dict) or not categories or not isinstance(mapping, dict)
            or any(not isinstance(mapping.get(category), list) or not mapping[category]
                   or not all(isinstance(part, str) and part.strip() and '/' not in part
                              for part in mapping[category]) for category in categories)):
        return '夸克分类对应的文件夹路径尚未配置完整'
    session_name = settings.get('session_file')
    if not isinstance(session_name, str) or not session_name or not Path(session_name).is_absolute():
        return '夸克登录记录须配置为可读取的绝对路径'
    try:
        session = Path(session_name)
        if not session.is_file() or session.stat().st_size > 2 * 1024 * 1024:
            return '本机夸克登录记录不可用，请重新登录'
        payload = json.loads(session.read_text(encoding='utf-8'))
        cookies = payload.get('cookies') if isinstance(payload, dict) else None
        if not isinstance(cookies, list) or not cookies or len(cookies) > 1000:
            return '本机夸克登录记录格式不完整，请重新登录'
        domains = {'quark.cn', 'pan.quark.cn', 'drive-pc.quark.cn', 'uop.quark.cn'}
        usable = False
        for cookie in cookies:
            if (not isinstance(cookie, dict)
                    or any(not isinstance(cookie.get(key), str) or not cookie[key] for key in ('domain', 'name', 'value'))
                    or not isinstance(cookie.get('path', '/'), str)):
                return '本机夸克登录记录格式不完整，请重新登录'
            usable = usable or cookie['domain'].lstrip('.') in domains
        if not usable:
            return '本机没有可用格式的夸克登录记录，请重新登录'
    except (OSError, ValueError, TypeError):
        return '本机夸克登录记录不可用，请重新登录'
    return None


def readiness(config, root, deep=False):
    downloaders = config.get('download_commands') or {}
    reasons = {}
    for platform in ('douyin', 'wechat'):
        # Keep the same precedence as collector.prepare.
        if platform == 'wechat' and (config.get('zhiling') or {}).get('enabled') is True:
            try:
                from zhiling_download import check_settings
                check_settings(config['zhiling'])
                reasons[platform] = None
            except Exception:
                reasons[platform] = '智凌密钥或视频校验工具未配置完整'
        elif platform == 'douyin' and (config.get('dtk') or {}).get('base_url'):
            try:
                from dtk_download import Client
                Client(config['dtk'])  # Validates URL/key, makes no request.
                reasons[platform] = None
            except Exception:
                reasons[platform] = '下载服务地址或授权配置不完整'
        else:
            command = downloaders.get(platform) if isinstance(downloaders, dict) else None
            reasons[platform] = command_problem(command or config.get('download_command'), deep)
    quark = quark_problem(config)
    categories = config.get('categories')
    categories_ready = (isinstance(categories, dict) and bool(categories)
                        and all(isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
                                for k, v in categories.items()))
    from storage_guard import status as storage_status
    return {**storage_status(config, root), 'platforms': {key: value is None for key, value in reasons.items()},
            'platform_problems': reasons, 'quark_ready': quark is None, 'quark_problem': quark,
            'categories_ready': bool(categories_ready), 'worker_online': worker_alive(root)}


def main(argv=None):
    parser = argparse.ArgumentParser(description='收藏助手本地运行检查（不会下载或上传）')
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
    parser.add_argument('--deep', action='store_true', help='同时检查抖音 Python 依赖能否加载')
    parser.add_argument('--platform', choices=('both', 'douyin', 'wechat'), default='both',
                        help='默认检查双平台；单平台检查须显式指定，不代表整体就绪')
    args = parser.parse_args(argv)
    try:
        config = load_runtime_config(args.config)
        root = Path(config.get('data_dir', 'data'))
        if not root.is_absolute():
            root = args.config.resolve().parent / root
        report = readiness(config, root, args.deep)
        required = ['douyin', 'wechat'] if args.platform == 'both' else [args.platform]
        report['checked_platforms'] = required
        report['local_checks_passed'] = bool(all(report['platforms'][name] for name in required)
                                            and report['quark_ready'] and report['categories_ready']
                                            and report['worker_online'] and report['storage_ready'])
        report['scope'] = '本地配置检查；未验证网络、夸克登录有效期或实际上传'
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['local_checks_passed'] else 1
    except (OSError, ValueError, TypeError, AttributeError):
        print(json.dumps({'error': '无法读取有效配置，请检查配置文件'}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
