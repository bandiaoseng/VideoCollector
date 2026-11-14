"""Phone API to default worker, with synthetic media and no external access."""
import hashlib
import ipaddress
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from collector import Queue
from web_app import create_app
from worker import run


class WebRuntimeTest(unittest.TestCase):
    def test_closing_page_does_not_stop_default_independent_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / 'fixture.mp4'
            fixture.write_bytes(b'\0\0\0\x18ftypmp42' + b'offline-media' * 100)
            session = root / 'session.json'
            session.write_text(json.dumps({'cookies': [{'domain': '.quark.cn', 'name': 'fixture',
                                                        'value': 'offline-only'}]}), encoding='utf-8')
            config = {'categories': {'二创': 'configured-id'}, 'data_dir': 'data',
                      'download_command': [sys.executable, '-c',
                          'import shutil,sys;shutil.copyfile(sys.argv[1],sys.argv[2])',
                          str(fixture), '{output}', '{source}']}
            config_path = root / 'config.json'
            config_path.write_text(json.dumps(config), encoding='utf-8')
            (root / '.runtime').mkdir()
            (root / '.runtime' / 'quark-web-settings.json').write_text(json.dumps({
                'session_file': str(session), 'category_paths': {'二创': ['搜集视频', '二创']}}), encoding='utf-8')
            stopped, page_closed = threading.Event(), threading.Event()
            calls = {'uploads': 0, 'closed': 0}
            owner = self
            class FakeClient:
                rows = []
                def __init__(self, path):
                    owner.assertEqual(Path(path), session)
                    self.http = SimpleNamespace(close=self.close)
                def close(self): calls['closed'] += 1
                def list_all(self, parent): return self.rows if parent == 'target' else []
                def resolve(self, parts):
                    owner.assertEqual(parts, ['搜集视频', '二创'])
                    return 'target'
                def upload(self, path, parent, progress):
                    owner.assertTrue(page_closed.wait(5), 'The page must close before upload proceeds')
                    calls['uploads'] += 1
                    hashes = {k: hashlib.new(k, path.read_bytes()).hexdigest() for k in ('md5', 'sha1', 'sha256')}
                    self.rows = [{'fid': 'offline-file', 'file_name': path.name, 'file': True,
                                  'size': path.stat().st_size, **hashes}]
                    stopped.set()
                    return hashes
            originals = (socket.socket.connect, socket.socket.connect_ex)
            def guarded(original):
                def connect(sock, address):
                    try:
                        if isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback:
                            return original(sock, address)
                    except ValueError:
                        pass
                    raise AssertionError('External network forbidden')
                return connect
            failures = []
            def work():
                try: run(config_path, stopped)
                except BaseException as exc: failures.append(exc)
            with (patch.object(socket.socket, 'connect', guarded(originals[0])),
                  patch.object(socket.socket, 'connect_ex', guarded(originals[1])),
                  patch('quark_web_uploader.WebClient', FakeClient),
                  patch('collector.QuarkUploader', side_effect=AssertionError('Legacy uploader forbidden'))):
                thread = threading.Thread(target=work)
                thread.start()
                try:
                    deadline = time.monotonic() + 5
                    while not (root / 'data' / 'worker-heartbeat.json').exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    app = create_app(config_path, 'offline-access-token-' + 'x' * 32, 'http://localhost')
                    headers = {'X-Collector-Request': '1'}
                    with TestClient(app, base_url='http://localhost') as browser:
                        self.assertEqual(browser.post('/api/login', json={'token': 'offline-access-token-' + 'x' * 32}, headers=headers).status_code, 200)
                        status = browser.get('/api/status').json()
                        self.assertTrue(status['quark_ready'])
                        self.assertTrue(status['worker_online'])
                        ident = str(uuid4())
                        created = browser.post('/api/drafts', json={'id': ident, 'text': 'https://v.douyin.com/offline-phone/'}, headers=headers)
                        self.assertEqual(created.status_code, 200)
                        submitted = browser.post(f'/api/drafts/{ident}/submit',
                            json={'note': '手机后台测试 #二创', 'version': 0}, headers=headers)
                        self.assertEqual(submitted.status_code, 200, submitted.text)
                        task_id = submitted.json()['task_id']
                    page_closed.set()
                    thread.join(8)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(failures, [])
                    queue = Queue(root / 'data')
                    try:
                        task = queue.get(task_id)
                        self.assertEqual(task['state'], 'succeeded')
                        self.assertEqual(Path(task['local_file']).name, '手机后台测试.mp4')
                        self.assertEqual(task['parent_fid'], 'configured-id')
                    finally:
                        queue.db.close()
                    self.assertEqual(calls, {'uploads': 1, 'closed': 1})
                finally:
                    page_closed.set(); stopped.set(); thread.join(10)


if __name__ == '__main__':
    unittest.main()
