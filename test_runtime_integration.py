"""Default worker pipeline using synthetic media and a fake WebClient, no services."""
import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from collector import Queue
from runtime_config import load_runtime_config
from service_check import readiness
from worker import run


class OneCycleStop:
    def __init__(self): self.checks = 0
    def is_set(self):
        self.checks += 1
        return self.checks > 1
    def wait(self, _): return True


class RuntimeIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / 'config.json'
        self.runtime = self.root / '.runtime'
        self.runtime.mkdir()
        self.session = self.root / 'synthetic-session.json'
        self.session.write_text(json.dumps({'cookies': [{'domain': '.quark.cn', 'name': 'fixture',
                                                        'value': 'synthetic-only'}]}), encoding='utf-8')
        self.web = {'session_file': str(self.session), 'category_paths': {'二创': ['搜集视频', '二创']}}
        self.config = {'categories': {'二创': 'stable-category-id'}, 'data_dir': 'data'}
        self.write_config()
        self.rows = []
        self.calls = {'upload': 0, 'close': 0, 'created': 0, 'resolve': []}
        owner = self
        class FakeWebClient:
            def __init__(self, session_path):
                owner.assertEqual(Path(session_path), owner.session)
                owner.calls['created'] += 1
                self.http = SimpleNamespace(close=self.close)
            def close(self): owner.calls['close'] += 1
            def list_all(self, parent): return list(owner.rows) if parent == 'original-folder' else []
            def resolve(self, parts):
                owner.calls['resolve'].append(parts)
                return 'original-folder'
            def upload(self, path, parent, progress):
                owner.assertEqual(parent, 'original-folder')
                owner.calls['upload'] += 1
                hashes = {key: hashlib.new(key, path.read_bytes()).hexdigest() for key in ('md5', 'sha1', 'sha256')}
                owner.rows = [{'fid': 'offline-fid', 'file_name': path.name,
                               'file': True, 'size': path.stat().st_size, **hashes}]
                return hashes
        self.fake_client = FakeWebClient
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'httpx.Client.request',
                       'urllib.request.OpenerDirector.open'):
            guard = patch(target, side_effect=AssertionError('External requests are forbidden'))
            guard.start(); self.addCleanup(guard.stop)

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')

    def local_settings(self):
        (self.runtime / 'quark-web-settings.json').write_text(json.dumps(self.web), encoding='utf-8')

    def queue_task(self, source=None):
        fixture = self.root / 'offline.mp4'
        fixture.write_bytes(b'\0\0\0\x18ftypmp42' + b'synthetic-video' * 64)
        if source:
            self.config['download_command'] = [sys.executable, '-c',
                'import shutil,sys;shutil.copyfile(sys.argv[1],sys.argv[2])', str(fixture), '{output}', '{source}']
            self.write_config()
        queue = Queue(self.root / 'data')
        try:
            return queue.submit_note(source or str(fixture), '后台测试 #二创', self.config['categories'])
        finally:
            queue.db.close()

    def state(self, ident):
        queue = Queue(self.root / 'data')
        try: return queue.get(ident)
        finally: queue.db.close()

    def test_default_worker_loads_local_settings_and_uses_independent_upload(self):
        self.local_settings()
        original_config = self.config_path.read_bytes()
        ident = self.queue_task()
        with (patch('quark_web_uploader.WebClient', self.fake_client),
              patch('collector.QuarkUploader', side_effect=AssertionError('Must not choose legacy CLI')),
              redirect_stdout(io.StringIO())):
            run(self.config_path, OneCycleStop())  # No process or uploader injection.
        self.assertEqual(self.state(ident)['state'], 'succeeded')
        self.assertEqual(self.calls['upload'], 1)
        self.assertEqual(self.calls['created'], 1)
        self.assertEqual(self.calls['close'], 1)
        self.assertEqual(self.calls['resolve'], [['搜集视频', '二创']])
        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertFalse((self.root / 'data' / 'worker-heartbeat.json').exists())

    def test_share_task_runs_default_pipeline_with_synthetic_downloader(self):
        self.config['quark_web'] = self.web
        ident = self.queue_task('https://v.douyin.com/offlineFixture/')
        with patch('quark_web_uploader.WebClient', self.fake_client), redirect_stdout(io.StringIO()):
            run(self.config_path, OneCycleStop())
        self.assertEqual(self.state(ident)['state'], 'succeeded')
        self.assertEqual(self.state(ident)['input_text'], '后台测试 #二创')
        self.assertEqual(self.calls['upload'], 1)

    def test_collector_cli_loads_shared_independent_settings(self):
        from collector import main
        self.local_settings()
        ident = self.queue_task()
        with (patch('quark_web_uploader.WebClient', self.fake_client),
              patch('collector.QuarkUploader', side_effect=AssertionError('No legacy CLI')),
              patch.object(sys, 'argv', ['collector.py', '--config', str(self.config_path), 'run']),
              redirect_stdout(io.StringIO())):
            main()
        self.assertEqual(self.state(ident)['state'], 'succeeded')
        self.assertEqual(self.calls['upload'], 1)
        self.assertEqual(self.calls['close'], 1)

    def test_explicit_invalid_web_configuration_is_not_overridden_or_downgraded(self):
        self.local_settings()
        self.config['quark_web'] = {}
        self.write_config()
        ident = self.queue_task()
        with (patch('quark_web_uploader.WebClient', side_effect=AssertionError('No session to open')),
              patch('collector.QuarkUploader', side_effect=AssertionError('No fallback')),
              redirect_stdout(io.StringIO())):
            run(self.config_path, OneCycleStop())
        self.assertEqual(self.state(ident)['state'], 'setup_required')
        self.assertEqual(load_runtime_config(self.config_path)['quark_web'], {})

    def test_hints_preserve_explicit_tools_and_do_not_enable_parser_or_write_config(self):
        self.config['node'] = 'explicit-node'
        self.config['zhiling'] = {'enabled': False, 'ffmpeg': 'explicit-ffmpeg'}
        self.write_config()
        original = self.config_path.read_bytes()
        (self.runtime / 'desktop-tools.json').write_text(json.dumps({key: sys.executable
            for key in ('node', 'ffmpeg', 'ffprobe')}), encoding='utf-8')
        loaded = load_runtime_config(self.config_path)
        self.assertEqual(loaded['node'], 'explicit-node')
        self.assertEqual(loaded['zhiling']['ffmpeg'], 'explicit-ffmpeg')
        self.assertEqual(loaded['zhiling']['ffprobe'], sys.executable)
        self.assertIs(loaded['zhiling']['enabled'], False)
        self.assertEqual(self.config_path.read_bytes(), original)

    def test_shared_readiness_uses_local_settings_without_network(self):
        self.local_settings()
        with patch('quark_web_uploader.WebClient', side_effect=AssertionError('Local check only')):
            report = readiness(load_runtime_config(self.config_path), self.root / 'data')
        self.assertTrue(report['quark_ready'])
        self.assertFalse(report['platforms']['wechat'])

    def test_broken_local_settings_fail_closed_and_do_not_rewrite_any_file(self):
        local = self.runtime / 'quark-web-settings.json'
        local.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError): load_runtime_config(self.config_path)
        self.assertEqual(local.read_text(encoding='utf-8'), '{broken')
        self.config['quark_web'] = self.web
        self.write_config()
        self.assertEqual(load_runtime_config(self.config_path)['quark_web'], self.web)


if __name__ == '__main__':
    unittest.main()
