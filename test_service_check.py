import json
import io
from contextlib import redirect_stdout
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from service_check import command_problem, main, quark_problem, readiness, worker_alive


class ServiceCheckTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.script = self.root / 'douyin_f2.py'
        self.script.write_text('# fixture', encoding='utf-8')
        self.command = [sys.executable, str(self.script), '--source', '{source}', '--output', '{output}',
                        '--ffprobe', sys.executable]
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'urllib.request.urlopen'):
            guard = patch(target, side_effect=AssertionError('No external network in local readiness checks'))
            guard.start(); self.addCleanup(guard.stop)

    def tearDown(self):
        self.temp.cleanup()

    def test_configured_but_missing_program_script_or_tool_is_not_ready(self):
        self.assertIsNone(command_problem(self.command))
        for command in ([str(self.root / 'missing.exe'), '{source}', '{output}'],
                        [sys.executable, str(self.root / 'missing.py'), '{source}', '{output}'],
                        self.command + ['--aria2', str(self.root / 'missing.exe')],
                        self.command + ['--aria2']):
            self.assertIsNotNone(command_problem(command))

    def test_malformed_command_and_missing_placeholder_are_rejected(self):
        for command in ('python downloader.py', [sys.executable, 123], [sys.executable, '{source}'],
                        [sys.executable, '{output}'], [sys.executable, '', '{source}', '{output}']):
            self.assertIsNotNone(command_problem(command))

    def test_deep_dependencies_checked_but_fast_status_never_executes(self):
        with patch('service_check.subprocess.run', return_value=subprocess.CompletedProcess([], 1)) as run:
            self.assertIsNone(command_problem(self.command))
            run.assert_not_called()
            self.assertIn('依赖', command_problem(self.command, deep=True))
            self.assertEqual(run.call_count, 1)
        with patch('service_check.subprocess.run', side_effect=subprocess.TimeoutExpired('python', 20)):
            self.assertIsNotNone(command_problem(self.command, deep=True))

    def test_dtk_precedence_invalid_key_or_url_does_not_fall_back(self):
        config = {'download_commands': {'douyin': self.command},
                  'dtk': {'base_url': 'http://127.0.0.1:8000', 'api_key_env': 'COLLECTOR_TEST_KEY'}}
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(readiness(config, self.root)['platforms']['douyin'])
        with patch.dict(os.environ, {'COLLECTOR_TEST_KEY': 'not-a-real-key'}):
            self.assertTrue(readiness(config, self.root)['platforms']['douyin'])
            config['dtk']['base_url'] = 'https://demo.douyin.wtf'
            self.assertFalse(readiness(config, self.root)['platforms']['douyin'])

    def test_status_no_paths_secrets_or_claim_of_live_authorization(self):
        config = {'download_commands': {'douyin': self.command}, 'categories': {'二创': 'private-id'},
                  'quark_cli': str(self.root / 'private-cli'), 'quark_authorized': True}
        report = readiness(config, self.root)
        self.assertEqual(report['platforms'], {'douyin': True, 'wechat': False})
        self.assertTrue(report['categories_ready'])
        self.assertFalse(report['quark_ready'])
        self.assertNotIn(str(self.root), json.dumps(report))
        self.assertNotIn('private-id', json.dumps(report))
        config['categories']['二创'] = ''
        self.assertFalse(readiness(config, self.root)['categories_ready'])

    def test_stale_future_invalid_and_missing_heartbeat_are_offline(self):
        heartbeat = self.root / 'worker-heartbeat.json'
        self.assertFalse(worker_alive(self.root))
        for value in (time.time() - 40, time.time() + 60, 'invalid', None):
            heartbeat.write_text(json.dumps({'updated': value}))
            self.assertFalse(worker_alive(self.root))
        heartbeat.write_text(json.dumps({'updated': time.time()}))
        self.assertTrue(worker_alive(self.root))

    def run_cli(self, platforms, *args):
        config = self.root / 'config.json'
        config.write_text('{"categories":{}}', encoding='utf-8')
        report = {'platforms': platforms, 'quark_ready': True, 'categories_ready': True,
                  'worker_online': True, 'storage_ready': True}
        output = io.StringIO()
        with patch('service_check.readiness', return_value=report), redirect_stdout(output):
            code = main(['--config', str(config), *args])
        return code, json.loads(output.getvalue())

    def test_default_check_cannot_pass_with_only_douyin_ready(self):
        code, report = self.run_cli({'douyin': True, 'wechat': False})
        self.assertEqual(code, 1)
        self.assertEqual(report['checked_platforms'], ['douyin', 'wechat'])
        self.assertFalse(report['local_checks_passed'])

    def test_partial_check_is_explicit_and_reports_its_scope(self):
        code, report = self.run_cli({'douyin': True, 'wechat': False}, '--platform', 'douyin')
        self.assertEqual(code, 0)
        self.assertEqual(report['checked_platforms'], ['douyin'])
        self.assertFalse(report['platforms']['wechat'])
        code, _ = self.run_cli({'douyin': True, 'wechat': False}, '--platform', 'wechat')
        self.assertEqual(code, 1)

    def test_both_local_checks_pass_without_claiming_live_acceptance(self):
        code, report = self.run_cli({'douyin': True, 'wechat': True})
        self.assertEqual(code, 0)
        self.assertTrue(report['local_checks_passed'])
        self.assertIn('未验证网络', report['scope'])

    def web_config(self):
        session = self.root / 'synthetic-session.json'
        session.write_text(json.dumps({'cookies': [{'domain': '.quark.cn', 'name': 'synthetic',
                                                   'value': 'offline-not-a-real-cookie'}]}), encoding='utf-8')
        return {'categories': {'二创': 'stable-category-id'}, 'quark_web': {
            'session_file': str(session), 'category_paths': {'二创': ['搜集视频', '二创']}}}

    def test_independent_readiness_does_not_construct_client_or_call_network_preflight(self):
        config = self.web_config()
        with (patch('quark_web_uploader.WebClient', side_effect=AssertionError('No client needed')),
             patch('quark_web_uploader.WebUploader.preflight', side_effect=AssertionError('No network preflight')),
             patch('service_check.QuarkUploader', side_effect=AssertionError('No legacy fallback'))):
            report = readiness(config, self.root)
        self.assertTrue(report['quark_ready'])
        self.assertIsNone(report['quark_problem'])
        serialized = json.dumps(report)
        for private in (str(self.root), 'stable-category-id', 'offline-not-a-real-cookie', '搜集视频'):
            self.assertNotIn(private, serialized)

    def test_malformed_independent_settings_never_use_legacy_backend(self):
        config = self.web_config()
        with patch('service_check.QuarkUploader', side_effect=AssertionError('No fallback')):
            for setting in (None, [], {}, 'invalid'):
                with self.subTest(setting=setting):
                    config['quark_web'] = setting
                    self.assertIsNotNone(quark_problem(config))

    def test_missing_or_bad_cookie_records_and_incomplete_mapping_are_not_ready(self):
        config = self.web_config()
        session = Path(config['quark_web']['session_file'])
        for payload in ('{broken', '{}', '{"cookies":[]}', '{"cookies":[{"domain":null}]}',
                        '{"cookies":[{"domain":"example.com","name":"n","value":"v"}]}'):
            with self.subTest(payload=payload):
                session.write_text(payload, encoding='utf-8')
                self.assertIsNotNone(quark_problem(config))
        config = self.web_config()
        for mapping in ({}, {'二创': []}, {'二创': ['bad/part']}, {'二创': 'wrong-type'}):
            config['quark_web']['category_paths'] = mapping
            self.assertIsNotNone(quark_problem(config))
        config = self.web_config()
        config['quark_web']['session_file'] = 'relative-session.json'
        self.assertIsNotNone(quark_problem(config))

    def test_missing_httpx_dependency_is_not_ready(self):
        with patch('service_check.importlib.util.find_spec', return_value=None):
            self.assertIn('依赖', quark_problem(self.web_config()))

    def test_legacy_config_still_uses_local_legacy_check(self):
        with patch('service_check.QuarkUploader') as legacy:
            self.assertIsNone(quark_problem({'categories': {'二创': 'legacy-id'}}))
            legacy.return_value.preflight.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
