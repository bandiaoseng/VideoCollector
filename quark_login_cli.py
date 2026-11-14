"""Explicit, bounded QR login for hosts without a desktop; never runs a worker."""
import argparse
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import uuid

from quark_web import WebClient, SessionBusy, session_access, close_client
from runtime_config import load_runtime_config


class LoginCancelled(Exception):
    pass


class LoginTimedOut(Exception):
    pass


def write_qr(value, path):
    """Write the QR as a private file without printing its URL or token."""
    import qrcode
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        qrcode.make(value).save(stream, format='PNG')


def session_path(config_path):
    config_path = Path(config_path).resolve()
    settings = load_runtime_config(config_path).get('quark_web')
    value = settings.get('session_file') if isinstance(settings, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise ValueError('请先在运行配置中设置 quark_web.session_file，再开始扫码登录。')
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('运行配置中的登录记录必须使用绝对路径。')
    if path.is_symlink() or path.is_dir():
        raise ValueError('登录记录路径不是普通文件，已停止。')
    return path


def validate_session(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError('登录结果未保存')
    saved = json.loads(path.read_text(encoding='utf-8'))
    cookies = saved.get('cookies') if isinstance(saved, dict) else None
    if not isinstance(cookies, list) or not 1 <= len(cookies) <= 1000:
        raise ValueError('登录结果不完整')
    allowed = {'quark.cn', 'pan.quark.cn', 'drive-pc.quark.cn', 'uop.quark.cn'}
    for cookie in cookies:
        if (not isinstance(cookie, dict)
                or any(not isinstance(cookie.get(key), str) or not cookie[key]
                       for key in ('name', 'value', 'domain'))
                or cookie['domain'].lstrip('.') not in allowed
                or not isinstance(cookie.get('path', '/'), str)
                or not cookie.get('path', '/').startswith('/')):
            raise ValueError('登录结果不完整')


def run_login(config_path, *, start_login=False, timeout=300, refresh_after=120,
              poll_interval=2, client_factory=WebClient, qr_writer=write_qr,
              clock=time.monotonic, wait=time.sleep, cancelled=lambda: False,
              emit=print):
    """Return a safe status; tests inject a synthetic client, clock and QR writer.

    A fresh client saves into an isolated temporary session. Only a successful
    QR confirmation and saved cookie record replace the configured session.
    """
    if start_login is not True:
        emit('尚未启动登录。只有明确添加 --start-login 才会获取二维码。')
        return 'not_started'
    if (type(timeout) not in (int, float) or not 1 <= timeout <= 900
            or type(refresh_after) not in (int, float) or not 1 <= refresh_after <= 120
            or type(poll_interval) not in (int, float) or not 0 < poll_interval <= 10):
        emit('等待时间配置无效，未启动登录。')
        return 'failed'
    client = None
    qr_path = None
    committed = False
    deadline = clock() + timeout

    def checkpoint():
        if cancelled():
            raise LoginCancelled()
        remaining = deadline - clock()
        if remaining <= 0:
            raise LoginTimedOut()
        return remaining

    try:
        checkpoint()
        destination = session_path(config_path)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        qr_path = destination.parent / ('.quark-login-' + uuid.uuid4().hex + '.png')
        # Staging prevents a failed drive check or interrupted login from
        # overwriting an existing working or damaged session record.
        with ExitStack() as resources:
            resources.enter_context(session_access(destination))
            directory = resources.enter_context(tempfile.TemporaryDirectory(prefix='.quark-login-', dir=destination.parent))
            staging = Path(directory) / 'session.json'
            client = client_factory(staging, load_session=False)
            def cleanup_client():
                try:
                    close_client(client)
                except Exception:
                    pass
            resources.callback(cleanup_client)
            request = client.request_json if hasattr(client, 'request_json') else None
            if request is not None:
                def bounded_request(*args, **kwargs):
                    remaining = checkpoint()
                    # qr_poll can make several HTTP calls, including directory
                    # pagination. Check the total deadline before every call.
                    kwargs['timeout'] = min(10, remaining)
                    result = request(*args, **kwargs)
                    checkpoint()
                    return result
                client.request_json = bounded_request
            emit('正在获取登录二维码。请用手机夸克扫码并确认；按 Ctrl+C 可取消。')
            refresh_count = 0
            while True:
                checkpoint()
                token, url = client.qr_start()
                checkpoint()
                qr_path.unlink(missing_ok=True)
                qr_writer(url, qr_path)
                # Desktop adapters render the QR in memory and create no file.
                # File-based adapters still receive restrictive POSIX permissions.
                if os.name != 'nt' and qr_path.exists():
                    qr_path.chmod(0o600)
                refresh_count += 1
                emit(('二维码已生成：' if refresh_count == 1 else '二维码已刷新，请重新打开图片：') + str(qr_path))
                emit('请在受信任的设备上打开这张图片；不要把二维码或登录记录发给他人。')
                qr_deadline = min(deadline, clock() + refresh_after)
                while True:
                    remaining = checkpoint()
                    until_refresh = qr_deadline - clock()
                    if until_refresh <= 0:
                        break
                    wait(min(poll_interval, remaining, until_refresh))
                    checkpoint()
                    if clock() >= qr_deadline:
                        break
                    if client.qr_poll(token):
                        checkpoint()
                        validate_session(staging)
                        if destination.is_symlink() or destination.is_dir():
                            raise ValueError('登录记录路径已变化')
                        if os.name != 'nt':
                            staging.chmod(0o600)
                        checkpoint()
                        staging.replace(destination)
                        committed = True
                        emit('夸克登录成功，登录记录已保存。可继续原任务，无需重新下载视频。')
                        return 'signed_in'
    except (KeyboardInterrupt, LoginCancelled):
        if committed:
            emit('登录已完成，登录记录已保存。')
            return 'signed_in'
        emit('已取消扫码登录，原登录记录保持不变。')
        return 'cancelled'
    except LoginTimedOut:
        emit('扫码等待已到时，原登录记录保持不变。需要时可再次明确启动登录。')
        return 'timed_out'
    except SessionBusy as exc:
        emit(str(exc))
        return 'busy'
    except Exception:
        # Exception text can contain QR tokens, cookies, or request URLs.
        if committed:
            emit('登录已完成，登录记录已保存。')
            return 'signed_in'
        emit('扫码登录未完成，原登录记录保持不变。请检查运行配置或网络后再启动。')
        return 'failed'
    finally:
        if qr_path is not None:
            try:
                qr_path.unlink(missing_ok=True)
            except OSError:
                emit('本次二维码图片未能自动删除，请删除刚才显示的本地 PNG 文件。')


@contextmanager
def cancellation_signals():
    """Let an ordinary terminal termination run the private-file cleanup."""
    previous = {}
    def cancel(signum, frame):
        raise LoginCancelled()
    try:
        for name in ('SIGINT', 'SIGTERM', 'SIGHUP'):
            signum = getattr(signal, name, None)
            if signum is not None:
                previous[signum] = signal.signal(signum, cancel)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv=None, **adapters):
    parser = argparse.ArgumentParser(description='收藏助手无桌面扫码登录；不会下载、上传或启动后台任务')
    parser.add_argument('--config', type=Path, required=True, help='运行配置文件路径')
    parser.add_argument('--start-login', action='store_true', help='明确开始一次扫码登录')
    parser.add_argument('--timeout', type=int, default=300, help='总等待秒数，1–900，默认 300')
    args = parser.parse_args(argv)
    with cancellation_signals():
        status = run_login(args.config, start_login=args.start_login, timeout=args.timeout, **adapters)
    return {'signed_in': 0, 'not_started': 2, 'failed': 1, 'timed_out': 3, 'cancelled': 130, 'busy': 4}[status]


if __name__ == '__main__':
    raise SystemExit(main())
