import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import socket
import ssl

from collector import Queue, process_one
from configure_zhiling import save_key
from process_runner import ProcessCancelled
from service_check import readiness
from test_collector import FakeUploader
from zhiling_download import (Client, NoRedirect, NoProbeRedirect, ZhilingError,
                              ConnectionFailure, check_parser_connection, download, media_url, select_video)

SOURCE = 'https://weixin.qq.com/sph/offline-fixture'
ENVELOPE = {'code': 200, 'msg': '解析成功', 'data': {
    'url': 'https://finder.video.qq.com/251/20302/stodownload?token=synthetic-token',
    'title': 'offline fixture', 'encryption': False, 'picsList': None, 'videosList': None}}


class Response(io.BytesIO):
    status = 200

    def __init__(self, body, **headers):
        super().__init__(body)
        self.headers = {'Content-Length': str(len(body)), 'Content-Type': 'video/mp4', **headers}


class FakeClient:
    def __init__(self, body):
        self.body = body
        self.envelope = copy.deepcopy(ENVELOPE)
        self.parse_count = self.media_count = 0
        self.headers = {}
        self.parse_failure = None
        self.connection_count = 0
        self.connection_failure = None

    def check_connection(self):
        self.connection_count += 1
        if self.connection_failure:
            raise self.connection_failure

    def parse(self, source):
        assert source == SOURCE
        self.parse_count += 1
        if self.parse_failure:
            raise self.parse_failure
        return self.envelope

    def media(self, url):
        assert url == ENVELOPE['data']['url']
        self.media_count += 1
        return Response(self.body, **self.headers)


class ZhilingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            raise unittest.SkipTest('Real offline media tests require FFmpeg and ffprobe')
        cls.fixture_dir = tempfile.TemporaryDirectory()
        path = Path(cls.fixture_dir.name) / 'fixture.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-v', 'error',
            '-f', 'lavfi', '-i', 'color=c=blue:s=32x32:r=5:d=1',
            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
            '-c:v', 'libx264', '-c:a', 'aac', '-shortest', str(path)], check=True, timeout=30)
        cls.payload = path.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'download.mp4'
        self.config = {'enabled': True, 'api_key_env': 'COLLECTOR_TEST_ZHILING_KEY'}
        for target in ('socket.socket.connect', 'socket.socket.connect_ex'):
            guard = patch(target, side_effect=AssertionError('Network forbidden in offline tests'))
            guard.start()
            self.addCleanup(guard.stop)
        env = patch.dict(os.environ, {'COLLECTOR_TEST_ZHILING_KEY': 'synthetic-key'})
        env.start()
        self.addCleanup(env.stop)

    def test_download_decodes_and_reuses_parse_without_another_charge(self):
        client = FakeClient(self.payload)
        report = download(self.config, SOURCE, self.output, client=client)
        self.assertEqual(self.output.read_bytes(), self.payload)
        self.assertTrue(report['audio_present'])
        self.assertGreater(report['duration'], 0)
        self.output.unlink()
        download(self.config, SOURCE, self.output, client=client)
        self.assertEqual(client.parse_count, 1)
        self.assertEqual(client.connection_count, 1)
        receipt = self.output.with_suffix('.zhiling.json').read_text(encoding='utf-8')
        self.assertNotIn('synthetic-token', receipt)
        self.assertNotIn('synthetic-key', receipt)

    def test_encrypted_unknown_or_gallery_results_do_not_download(self):
        for change in ({'encryption': True}, {'encryption': None}, {'encryption': 0},
                       {'picsList': ['cover']}, {'videosList': ['one', 'two']}):
            envelope = copy.deepcopy(ENVELOPE)
            envelope['data'].update(change)
            with self.subTest(change=change), self.assertRaises(ZhilingError):
                select_video(envelope)
        for envelope in ({'code': 500, 'msg': 'secret'}, [], {'code': True}, {'code': 200, 'data': None}):
            with self.assertRaises(ZhilingError):
                select_video(envelope)

    def test_preview_foreign_urls_and_credentials_are_rejected(self):
        for url in ('https://channels.weixin.qq.com/preview', 'http://finder.video.qq.com/stodownload',
                    'https://finder.video.qq.com.evil.example/stodownload',
                    'https://secret@finder.video.qq.com/stodownload',
                    'https://finder.video.qq.com:444/stodownload',
                    'https://finder.video.qq.com/stodownload#fragment', None):
            with self.subTest(url=url), self.assertRaises(ZhilingError):
                media_url(url)

    def test_incomplete_or_oversized_transfer_never_publishes(self):
        for index, (length, limit) in enumerate(((len(self.payload) + 1, 100000), (len(self.payload), 10))):
            client = FakeClient(self.payload)
            client.headers = {'Content-Length': str(length)}
            output = self.root / f'case{index}.mp4'
            with self.assertRaises(ZhilingError):
                download(dict(self.config, max_bytes=limit), SOURCE, output, client=client)
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix('.zhiling-part').exists())

    def test_html_and_fake_mp4_are_not_uploaded(self):
        for index, body in enumerate((b'<html>failure</html>', b'\x00\x00\x00\x18ftypmp42' + bytes(40))):
            output = self.root / f'bad{index}.mp4'
            with self.assertRaises(ZhilingError):
                download(self.config, SOURCE, output, client=FakeClient(body))
            self.assertFalse(output.exists())

    def test_ambiguous_request_is_not_automatically_rebilled(self):
        client = FakeClient(self.payload)
        client.parse_failure = TimeoutError('synthetic timeout')
        with self.assertRaises(TimeoutError):
            download(self.config, SOURCE, self.output, client=client)
        client.parse_failure = None
        with self.assertRaisesRegex(ZhilingError, '结果不明'):
            download(self.config, SOURCE, self.output, client=client)
        self.assertEqual(client.parse_count, 1)
        self.assertEqual(client.media_count, 0)

    def test_cached_source_cannot_be_reassigned(self):
        client = FakeClient(self.payload)
        download(self.config, SOURCE, self.output, client=client)
        with self.assertRaisesRegex(ZhilingError, '不一致'):
            download(self.config, SOURCE + 'other', self.output, client=client)
        self.assertEqual(client.parse_count, 1)

    def test_missing_tools_or_key_stop_before_any_api_request(self):
        for override in ({'ffmpeg': 'nonexistent-collector-tool'}, {'api_key_env': 'COLLECTOR_MISSING_KEY'},
                         {'enabled': False}, {'max_bytes': -1}):
            client = FakeClient(self.payload)
            with self.subTest(override=override), self.assertRaises(ZhilingError):
                download(dict(self.config, **override), SOURCE, self.output, client=client)
            self.assertEqual(client.parse_count, 0)

    def test_request_contract_and_redirects_do_not_leak_secrets(self):
        client = Client(self.config)
        with patch.object(client.opener, 'open', return_value=Response(json.dumps(ENVELOPE).encode())) as call:
            self.assertEqual(client.parse(SOURCE), ENVELOPE)
            url = call.call_args.args[0].full_url
            self.assertTrue(url.startswith('https://api.17zhiling.com/api/video/parse-wechat-url?'))
            self.assertIn('key=synthetic-key', url)
        error = urllib.error.HTTPError('https://example.invalid/?secret', 403, 'synthetic-key', {}, None)
        with patch.object(client.opener, 'open', side_effect=error), self.assertRaises(ZhilingError) as caught:
            client.parse(SOURCE)
        self.assertNotIn('synthetic-key', str(caught.exception))
        with self.assertRaises(ZhilingError):
            NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.invalid/')

    def test_connection_errors_identify_stage_without_exposing_secret_urls(self):
        cases = [(socket.gaierror(11001, 'synthetic-key'), 'dns'),
                 (TimeoutError('https://secret.invalid?key=synthetic-key'), 'timeout'),
                 (ssl.SSLError('synthetic-key'), 'tls'),
                 (ConnectionResetError(10054, 'synthetic-key'), 'connection')]
        client=Client(self.config)
        for reason, kind in cases:
            with self.subTest(kind=kind), patch.object(client.opener,'open',side_effect=urllib.error.URLError(reason)):
                with self.assertRaises(ConnectionFailure) as caught:client.parse(SOURCE)
                self.assertEqual(caught.exception.details['stage'],'parse')
                self.assertEqual(caught.exception.details['kind'],kind)
                self.assertNotIn('synthetic-key',str(caught.exception))
                self.assertNotIn('https://',str(caught.exception))
                with self.assertRaises(ConnectionFailure) as media_error:client.media(ENVELOPE['data']['url'])
                self.assertEqual(media_error.exception.details['stage'],'media')
                self.assertIn('不会重新解析',str(media_error.exception))

    def test_parser_response_read_timeout_is_classified_as_parse_failure(self):
        client=Client(self.config)
        response=Response(b'')
        with patch.object(client.opener,'open',return_value=response), patch.object(response,'read',side_effect=TimeoutError('secret')):
            with self.assertRaises(ConnectionFailure) as caught:client.parse(SOURCE)
        self.assertEqual(caught.exception.details,{'stage':'parse','kind':'timeout','errno':None})

    def test_connection_probe_never_reads_key_or_sends_video_or_follows_redirects(self):
        with patch('zhiling_download.read_key', side_effect=AssertionError('No key for probe')), \
                patch('urllib.request.OpenerDirector.open', return_value=Response(b'')) as opened:
            result = check_parser_connection()
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.17zhiling.com/')
        self.assertEqual(request.get_method(), 'HEAD')
        self.assertNotIn('Authorization', request.headers)
        self.assertFalse(result['parser_called'])
        self.assertIsNone(NoProbeRedirect().redirect_request(None, None, 302, '', {}, 'https://example.invalid/'))

    def test_connection_probe_reports_tls_eof_without_request_data(self):
        reason = ssl.SSLEOFError(8, 'https://secret.invalid/?key=synthetic-key')
        with patch('urllib.request.OpenerDirector.open', side_effect=urllib.error.URLError(reason)):
            with self.assertRaises(ConnectionFailure) as caught:
                check_parser_connection()
        self.assertEqual(caught.exception.details,
                         {'stage': 'preflight', 'kind': 'tls', 'errno': 8, 'tls_reason': 'unexpected_eof'})
        self.assertIn('没有调用解析接口', str(caught.exception))
        self.assertNotIn('synthetic-key', str(caught.exception))

    def test_connection_probe_accepts_head_rejection_but_stops_on_server_failure(self):
        for status in (302, 405, 503):
            with self.subTest(status=status):
                error = urllib.error.HTTPError('https://api.17zhiling.com/', status, 'upstream', {}, None)
                with patch('urllib.request.OpenerDirector.open', side_effect=error):
                    if status == 503:
                        with self.assertRaisesRegex(ZhilingError, '没有调用解析接口'):
                            check_parser_connection()
                    else:
                        self.assertEqual(check_parser_connection()['http_status'], status)

    def test_failed_connection_check_preserves_unstarted_parse_and_continues_once(self):
        client = FakeClient(self.payload)
        client.connection_failure = ConnectionFailure(ssl.SSLEOFError(8, 'secret'), 'preflight')
        with self.assertRaises(ConnectionFailure):
            download(self.config, SOURCE, self.output, client=client)
        self.assertEqual(client.parse_count, 0)
        self.assertEqual(client.media_count, 0)
        self.assertFalse(self.output.with_suffix('.zhiling-private.json').exists())
        client.connection_failure = None
        download(self.config, SOURCE, self.output, client=client)
        self.assertEqual(client.parse_count, 1)

    def test_configuration_saves_privately_without_enabling_or_changing_douyin(self):
        path = self.root / 'config.json'
        original = {'download_commands': {'douyin': ['existing']}, 'categories': {'二创': 'test-folder'}}
        path.write_text(json.dumps(original), encoding='utf-8')
        key_path = save_key(path, 'synthetic-new-key')
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(key_path.read_text(), 'synthetic-new-key')
        self.assertFalse(saved['zhiling']['enabled'])
        self.assertEqual(saved['download_commands'], original['download_commands'])
        self.assertNotIn('synthetic-new-key', path.read_text(encoding='utf-8'))
        saved['zhiling']['enabled'] = True
        path.write_text(json.dumps(saved), encoding='utf-8')
        with self.assertRaises(ValueError):
            save_key(path, 'replacement')
        self.assertEqual(key_path.read_text(), 'synthetic-new-key')

    def test_local_status_checks_key_and_tools_without_network(self):
        config = {'zhiling': self.config}
        self.assertTrue(readiness(config, self.root)['platforms']['wechat'])
        config['zhiling'] = dict(self.config, ffprobe='nonexistent-collector-tool')
        self.assertFalse(readiness(config, self.root)['platforms']['wechat'])

    def test_two_platforms_route_to_same_naming_and_upload_pipeline_offline(self):
        queue = Queue(self.root / 'queue')
        self.addCleanup(queue.db.close)
        config = {'zhiling': self.config, 'dtk': {'base_url': 'https://offline.example'}}
        queue.submit_note(SOURCE, '视频号点评 #二创', {'二创': 'test-folder'})
        queue.submit_note('https://v.douyin.com/offline/', '抖音点评 #二创', {'二创': 'test-folder'})
        client = FakeClient(self.payload)
        uploader = FakeUploader()

        def run_offline(args, **kwargs):
            self.assertNotIn('synthetic-key', ' '.join(args))
            settings = json.loads(args[args.index('--settings-json') + 1])
            output = Path(args[args.index('--output') + 1])
            download(settings, args[args.index('--source') + 1], output, client=client)
            return subprocess.CompletedProcess(args, 0, '', '')

        def douyin_offline(config, source, output, cancel_check=None):
            self.assertTrue(callable(cancel_check))
            self.assertFalse(cancel_check())
            Path(output).write_bytes(self.payload)

        with patch('collector.run_managed', side_effect=run_offline), patch('dtk_download.download', side_effect=douyin_offline):
            self.assertEqual(process_one(queue, config, uploader)['state'], 'succeeded')
            self.assertEqual(process_one(queue, config, uploader)['state'], 'succeeded')
        self.assertEqual(uploader.calls, [('视频号点评.mp4', 'test-folder'), ('抖音点评.mp4', 'test-folder')])
        self.assertEqual(client.parse_count, 1)

    def test_cancelled_managed_downloader_cannot_upload(self):
        queue = Queue(self.root / 'queue')
        self.addCleanup(queue.db.close)
        task_id = queue.submit_note(SOURCE, '点评 #二创', {'二创': 'test-folder'})
        uploader = FakeUploader()

        def cancel(*args, **kwargs):
            queue.cancel(task_id)
            raise ProcessCancelled('cancelled fixture')

        with patch('collector.run_managed', side_effect=cancel):
            result = process_one(queue, {'zhiling': self.config}, uploader)
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(uploader.calls, [])


if __name__ == '__main__':
    unittest.main()
