"""Desktop integration tests: synthetic provider and upload responses only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import ssl
import unittest
from unittest.mock import patch

from collector import Queue, WorkerLock
from desktop_app import build_window, job_key, plan_request, result_text, run_request
from destination_check import NameConflict
from test_collector import FakeUploader
from test_zhiling_download import FakeClient, SOURCE
from zhiling_download import download, ConnectionFailure, ZhilingError


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            raise unittest.SkipTest('FFmpeg required for offline video fixture')
        cls.media_dir = tempfile.TemporaryDirectory()
        fixture = Path(cls.media_dir.name) / 'fixture.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-v', 'error',
                        '-f', 'lavfi', '-i', 'color=c=blue:s=32x32:r=5:d=1',
                        '-c:v', 'libx264', str(fixture)], check=True, timeout=30)
        cls.payload = fixture.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.media_dir.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / 'config.json'
        self.config = {'categories': {'二创': 'offline-folder'},
                       'zhiling': {'enabled': False, 'api_key_env': 'COLLECTOR_DESKTOP_TEST_KEY'}}
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')
        self.original_config = self.config_path.read_bytes()
        self.client = FakeClient(self.payload)
        self.uploader = FakeUploader()
        self.note = '自动收藏窗口测试 #二创'
        for target in ('socket.socket.connect', 'socket.socket.connect_ex'):
            guard = patch(target, side_effect=AssertionError('Network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        env = patch.dict(os.environ, {'COLLECTOR_DESKTOP_TEST_KEY': 'synthetic-key'})
        env.start()
        self.addCleanup(env.stop)
        adapter = patch('collector.run_managed', side_effect=self.offline_download_process)
        adapter.start()
        self.addCleanup(adapter.stop)
        connection = patch('desktop_app.check_parser_connection',
                           return_value={'parser_called': False, 'http_status': 200})
        self.connection_check = connection.start()
        self.addCleanup(connection.stop)

    def offline_download_process(self, args, **kwargs):
        self.assertTrue(args[1].endswith('zhiling_download.py'))
        self.assertNotIn('synthetic-key', ' '.join(args))
        settings = json.loads(args[args.index('--settings-json') + 1])
        output = Path(args[args.index('--output') + 1])
        try:
            download(settings, args[args.index('--source') + 1], output, client=self.client)
        except ZhilingError as exc:
            output.with_suffix('.zhiling-error.json').write_text(json.dumps(
                {'error': str(exc), 'details': getattr(exc, 'details', None)}), encoding='utf-8')
            return subprocess.CompletedProcess(args, 1, json.dumps({'error': str(exc)}), '')
        return subprocess.CompletedProcess(args, 0, '', '')

    def run_sample(self, **kwargs):
        return run_request(self.config_path, SOURCE, self.note,
                           uploader_factory=getattr(self, 'uploader_factory',
                               lambda config, progress: self.uploader), **kwargs)

    def test_link_routes_through_download_naming_and_upload_without_changing_live_config(self):
        report = self.run_sample()
        self.assertEqual(report['state'], 'succeeded')
        self.assertEqual(self.uploader.calls, [('自动收藏窗口测试.mp4', 'offline-folder')])
        self.assertEqual(self.client.parse_count, 1)
        self.assertEqual(self.config_path.read_bytes(), self.original_config)
        saved = Path(report['report_path']).read_text(encoding='utf-8')
        self.assertNotIn('synthetic-key', saved)
        self.assertNotIn('synthetic-token', saved)

    def test_repeat_after_restart_returns_same_result_without_parser_or_upload(self):
        first = self.run_sample()
        second = self.run_sample()
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(second['state'], 'succeeded')
        self.assertEqual(self.client.parse_count, 1)
        self.assertEqual(len(self.uploader.calls), 1)

    def test_ambiguous_parser_is_not_rebilled_on_explicit_retry(self):
        self.client.parse_failure = TimeoutError('offline timeout')
        first = self.run_sample()
        self.assertEqual(first['state'], 'prepare_failed')
        self.client.parse_failure = None
        second = self.run_sample(retry=True)
        self.assertEqual(second['state'], 'prepare_failed')
        self.assertIn('结果不明', second['error'])
        self.assertEqual(self.client.parse_count, 1)
        self.assertEqual(self.uploader.calls, [])

    def test_unknown_upload_never_reuploads_on_restart_or_retry(self):
        self.uploader.fail = True
        self.assertEqual(self.run_sample()['state'], 'needs_review')
        self.assertEqual(self.run_sample(retry=True)['state'], 'needs_review')
        self.assertEqual(len(self.uploader.calls), 1)

    def test_separate_approved_reparse_preserves_old_attempt_and_only_calls_once(self):
        self.client.parse_failure = TimeoutError('offline timeout')
        first = self.run_sample()
        self.assertTrue(first['parse_retry_required'])
        self.client.parse_failure = None
        result = self.run_sample(reparse=True)
        self.assertEqual(result['id'], first['id'])
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(self.client.parse_count, 2)
        folder = Path(result['report_path']).parent / 'files' / result['id']
        history = list(folder.glob('download.zhiling-private.attempt-*.json'))
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0].read_text())['state'], 'request_started')
        with self.assertRaises(ValueError): self.run_sample(reparse=True)
        self.assertEqual(self.client.parse_count, 2)

    def test_failed_connection_check_keeps_ambiguous_attempt_and_does_not_parse(self):
        self.client.parse_failure = TimeoutError('offline timeout')
        first = self.run_sample()
        folder = Path(first['report_path']).parent / 'files' / first['id']
        original = (folder / 'download.zhiling-private.json').read_bytes()
        self.connection_check.side_effect = ConnectionFailure(ssl.SSLEOFError(8, 'secret'), 'preflight')
        result = self.run_sample(reparse=True)
        self.assertEqual(result['state'], 'prepare_failed')
        self.assertTrue(result['parse_retry_required'])
        self.assertTrue(result['connection_check_failed'])
        self.assertIn('本次未重新解析', result_text(result))
        self.assertEqual(self.client.parse_count, 1)
        self.assertEqual((folder / 'download.zhiling-private.json').read_bytes(), original)
        self.assertEqual(list(folder.glob('download.zhiling-private.attempt-*.json')), [])
        diagnostic = json.loads((folder / 'connection-check.json').read_text(encoding='utf-8'))
        self.assertFalse(diagnostic['parser_called'])
        self.assertEqual(diagnostic['details']['tls_reason'], 'unexpected_eof')
        self.connection_check.side_effect = None
        self.client.parse_failure = None
        resumed = self.run_sample(reparse=True)
        self.assertEqual(resumed['id'], first['id'])
        self.assertEqual(resumed['state'], 'succeeded')
        self.assertEqual(self.client.parse_count, 2)

    def test_connection_lost_after_first_check_keeps_explicit_reparse_requirement(self):
        self.client.parse_failure = TimeoutError('offline timeout')
        first = self.run_sample()
        self.client.parse_failure = None
        self.client.connection_failure = ConnectionFailure(ssl.SSLEOFError(8, 'secret'), 'preflight')
        result = self.run_sample(reparse=True)
        self.assertTrue(result['parse_retry_required'])
        self.assertTrue(result['connection_check_failed'])
        self.assertEqual(self.client.parse_count, 1)
        self.client.connection_failure = None
        self.assertEqual(self.run_sample(retry=True)['state'], 'prepare_failed')
        self.assertEqual(self.client.parse_count, 1)
        self.assertEqual(self.run_sample(reparse=True)['state'], 'succeeded')
        self.assertEqual(self.client.parse_count, 2)

    def test_unexpected_uploader_setup_error_preserves_original_parse_barrier(self):
        self.client.parse_failure = TimeoutError('offline timeout')
        first = self.run_sample()
        folder = Path(first['report_path']).parent / 'files' / first['id']
        original = (folder / 'download.zhiling-private.json').read_bytes()
        def unavailable(*args):
            raise ValueError('offline setup error')
        result = run_request(self.config_path, SOURCE, self.note,
                             uploader_factory=unavailable, reparse=True)
        self.assertEqual(result['state'], 'setup_required')
        self.assertIn('offline setup error', result['error'])
        self.assertEqual((folder / 'download.zhiling-private.json').read_bytes(), original)
        self.client.parse_failure = None
        self.assertEqual(self.run_sample(retry=True)['state'], 'prepare_failed')
        self.assertEqual(self.client.parse_count, 1)

    def test_note_spacing_reuses_old_task_and_does_not_parse_again(self):
        first = self.run_sample()
        self.note = '自动收藏窗口测试    #二创'
        second = self.run_sample()
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(self.client.parse_count, 1)
        self.assertEqual(len(self.uploader.calls), 1)

    def test_desktop_conflict_correction_reuses_video_without_parsing(self):
        with patch.object(self.uploader, 'check_destination', side_effect=NameConflict()):
            first = self.run_sample()
        original = Path(first['report_path']).parent / 'files' / first['id'] / first['filename']
        self.note = '修改后的点评 #二创'
        result = self.run_sample(revise_from={'job_key': first['job_key'], 'task_id': first['id']})
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['filename'], '修改后的点评.mp4')
        self.assertEqual(self.client.parse_count, 1)
        self.assertEqual(self.client.media_count, 1)
        self.assertEqual(original.read_bytes(), self.payload)
        self.assertEqual(self.uploader.calls, [('修改后的点评.mp4', 'offline-folder')])

    def test_unknown_upload_reserves_filename_across_desktop_jobs(self):
        self.uploader.fail = True
        first = self.run_sample()
        self.assertEqual(first['state'], 'needs_review')
        with self.assertRaisesRegex(ValueError, '核对'):
            run_request(self.config_path, SOURCE + 'different', self.note,
                        uploader_factory=lambda config, progress: self.uploader)
        self.assertEqual(self.client.parse_count, 1)
        self.assertEqual(len(self.uploader.calls), 1)

    def test_rejected_revision_cannot_turn_into_fresh_parse_after_restart(self):
        with patch.object(self.uploader, 'check_destination', side_effect=NameConflict()):
            first = self.run_sample()
        reference = {'job_key': first['job_key'], 'task_id': first['id']}
        self.note = '第一次修改 #二创'
        self.assertEqual(self.run_sample(revise_from=reference)['state'], 'succeeded')
        self.note = '第二次修改 #二创'
        with self.assertRaisesRegex(ValueError, '另一条'):
            self.run_sample(revise_from=reference)
        for options in ({}, {'retry': True}):
            with self.assertRaisesRegex(ValueError, '另一条'):
                self.run_sample(**options)
        self.assertEqual(self.client.parse_count, 1)
        self.assertEqual(self.client.media_count, 1)
        self.assertEqual(len(self.uploader.calls), 1)

    def test_revision_interrupted_before_helper_retains_local_only_intent(self):
        with patch.object(self.uploader, 'check_destination', side_effect=NameConflict()):
            first = self.run_sample()
        self.note = '崩溃后续接 #二创'
        with patch('desktop_app.guard_pending_uploads', side_effect=ValueError('模拟中断')):
            with self.assertRaisesRegex(ValueError, '模拟中断'):
                self.run_sample(revise_from={'job_key': first['job_key'], 'task_id': first['id']})
        self.assertEqual(self.run_sample()['state'], 'succeeded')
        self.assertEqual(self.client.parse_count, 1)
        self.assertEqual(self.client.media_count, 1)
        self.assertEqual(len(self.uploader.calls), 1)

    def test_damaged_revision_source_never_reparses_on_ordinary_continue(self):
        with patch.object(self.uploader, 'check_destination', side_effect=NameConflict()):
            first = self.run_sample()
        original = Path(first['report_path']).parent / 'files' / first['id'] / first['filename']
        original.write_bytes(b'corrupted offline fixture')
        self.note = '修改后不重新下载 #二创'
        with self.assertRaisesRegex(ValueError, '损坏'):
            self.run_sample(revise_from={'job_key': first['job_key'], 'task_id': first['id']})
        with self.assertRaisesRegex(ValueError, '损坏'):
            self.run_sample(retry=True)
        self.assertEqual(self.client.parse_count, 1)
        self.assertEqual(self.client.media_count, 1)
        self.assertEqual(self.uploader.calls, [])

    def test_conflict_window_offers_cached_revision_and_disables_new_start(self):
        import tkinter as tk
        with patch.object(self.uploader, 'check_destination', side_effect=NameConflict()):
            first = self.run_sample()
        form = self.root / '.runtime' / 'desktop-form.json'
        form.write_text(json.dumps({'source': SOURCE, 'note': self.note}), encoding='utf-8')
        calls = []
        def fake_runner(path, text, note, **kwargs):
            calls.append(kwargs)
            return {**first, 'state': 'succeeded', 'filename': '新点评.mp4', 'full_path': '夸克网盘/离线/二创'}
        try:
            window = build_window(self.config_path, runner=fake_runner)
        except tk.TclError as exc:
            self.skipTest(str(exc))
        try:
            window.withdraw()
            deadline = time.monotonic() + .4
            while time.monotonic() < deadline:
                window.update(); time.sleep(.02)
            c = window.desktop_controls
            c['note'].delete('1.0', 'end'); c['note'].insert('1.0', '新点评 #二创')
            window.update()
            self.assertTrue(c['start'].instate(['disabled']))
            self.assertTrue(c['revise'].instate(['!disabled']))
            c['revise'].invoke()
            deadline = time.monotonic() + 3
            while c['state']['busy'] and time.monotonic() < deadline:
                window.update(); time.sleep(.02)
            self.assertEqual(calls[0]['revise_from'], {'job_key': first['job_key'], 'task_id': first['id']})
            self.assertEqual(self.client.parse_count, 1)
        finally:
            window.destroy()

    def standalone_unknown_upload(self, *, remote_hashes=True):
        from quark_web import WebError
        from quark_web_uploader import WebUploader
        from test_quark_recovery import FakeWeb
        class DropFinalListing(FakeWeb):
            drop = True
            def list_all(self, parent):
                if self.upload_calls and self.drop:
                    raise WebError('模拟：上传完成后的目录检查断网')
                return super().list_all(parent)
        web = DropFinalListing()
        web.allow_upload = True
        session = self.root / 'session.json'
        session.write_text('{"cookies":[]}', encoding='utf-8')
        self.config['quark_web'] = {'session_file': str(session),
                                   'category_paths': {'二创': ['搜集视频', '二创']}}
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')
        self.uploader_factory = lambda config, progress: WebUploader(config, progress, client=web)
        self.assertEqual(self.run_sample()['state'], 'needs_review')
        self.assertEqual(web.upload_calls, 1)
        web.drop = False
        if not remote_hashes:
            for row in web.rows:
                row.pop('md5', None); row.pop('sha1', None)
        return web

    def test_standalone_readonly_review_completes_unknown_upload_without_repeating_work(self):
        web = self.standalone_unknown_upload()
        result = self.run_sample(review=True)
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['full_path'], '夸克网盘/搜集视频/二创')
        self.assertEqual(web.upload_calls, 1)
        self.assertEqual(self.client.parse_count, 1)
        self.assertEqual(self.client.media_count, 1)
        self.assertEqual(self.run_sample()['state'], 'succeeded')
        self.assertEqual(web.upload_calls, 1)

    def test_standalone_missing_hash_requires_explicit_candidate_confirmation(self):
        web = self.standalone_unknown_upload(remote_hashes=False)
        first = self.run_sample(review=True)
        self.assertEqual(first['state'], 'needs_review')
        self.assertTrue(first.get('review_confirmation'))
        self.assertEqual(self.run_sample(retry=True)['state'], 'needs_review')
        result = self.run_sample(review=True, confirmation=first['review_confirmation'])
        self.assertEqual(result['state'], 'succeeded')
        self.assertIn('明确确认', result['notice'])
        self.assertEqual(web.upload_calls, 1)
        self.assertEqual(self.client.parse_count, 1)

    def test_review_window_confirmation_cancel_and_accept_are_explicit(self):
        import tkinter as tk
        web = self.standalone_unknown_upload(remote_hashes=False)
        report = self.run_sample(review=True)
        form = self.root / '.runtime' / 'desktop-form.json'
        form.write_text(json.dumps({'source': SOURCE, 'note': self.note}), encoding='utf-8')
        calls = []
        def fake_runner(*args, **kwargs):
            calls.append(kwargs)
            return report
        try:
            window = build_window(self.config_path, runner=fake_runner)
        except tk.TclError as exc:
            self.skipTest(str(exc))
        try:
            window.withdraw()
            deadline = time.monotonic() + .4
            while time.monotonic() < deadline:
                window.update(); time.sleep(.02)
            c = window.desktop_controls
            self.assertTrue(c['review'].instate(['!disabled']))
            self.assertTrue(c['confirm'].instate(['!disabled']))
            self.assertTrue(c['start'].instate(['disabled']))
            with patch('tkinter.messagebox.askyesno', return_value=False):
                c['confirm'].invoke()
            self.assertEqual(calls, [])
            with patch('tkinter.messagebox.askyesno', return_value=True):
                c['confirm'].invoke()
            deadline = time.monotonic() + 3
            while c['state']['busy'] and time.monotonic() < deadline:
                window.update(); time.sleep(.02)
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0]['review'])
            self.assertEqual(calls[0]['confirmation'], report['review_confirmation'])
            self.assertEqual(web.upload_calls, 1)
        finally:
            window.destroy()

    def test_rejected_new_input_does_not_disable_original_review_controls(self):
        import tkinter as tk
        self.standalone_unknown_upload()
        report = self.run_sample()
        form = self.root / '.runtime' / 'desktop-form.json'
        form.write_text(json.dumps({'source': SOURCE, 'note': self.note}), encoding='utf-8')
        def rejected(*args, **kwargs):
            raise ValueError('原任务仍需核对，本次未解析或上传')
        try:
            window = build_window(self.config_path, runner=rejected)
        except tk.TclError as exc:
            self.skipTest(str(exc))
        try:
            window.withdraw()
            deadline = time.monotonic() + .4
            while time.monotonic() < deadline:
                window.update(); time.sleep(.02)
            c = window.desktop_controls
            c['source'].delete('1.0', 'end'); c['source'].insert('1.0', SOURCE + 'different')
            window.update()
            c['start'].invoke()
            deadline = time.monotonic() + 3
            while c['state']['busy'] and time.monotonic() < deadline:
                window.update(); time.sleep(.02)
            c['source'].delete('1.0', 'end'); c['source'].insert('1.0', SOURCE)
            window.update()
            self.assertEqual(c['state']['last']['id'], report['id'])
            self.assertTrue(c['start'].instate(['disabled']))
            self.assertTrue(c['review'].instate(['!disabled']))
            self.assertEqual(self.client.parse_count, 1)
        finally:
            window.destroy()

    def test_reopening_restores_connection_failure_without_check_or_reparse(self):
        import tkinter as tk
        self.client.parse_failure = TimeoutError('offline timeout')
        self.run_sample()
        self.connection_check.side_effect = ConnectionFailure(ssl.SSLEOFError(8, 'secret'), 'preflight')
        report = self.run_sample(reparse=True)
        form = self.root / '.runtime' / 'desktop-form.json'
        form.write_text(json.dumps({'source': SOURCE, 'note': self.note}), encoding='utf-8')
        calls = self.connection_check.call_count
        try:
            window = build_window(self.config_path, runner=lambda *a, **kw: self.fail('No auto-start'))
        except tk.TclError as exc:
            self.skipTest(str(exc))
        try:
            window.withdraw()
            deadline = time.monotonic() + .4
            while time.monotonic() < deadline:
                window.update(); time.sleep(.02)
            controls = window.desktop_controls
            self.assertEqual(controls['state']['last']['id'], report['id'])
            self.assertIn('本次未重新解析', controls['status'].get())
            self.assertIn('加密连接被中途关闭', controls['output'].get('1.0', 'end'))
            self.assertTrue(controls['reparse'].instate(['!disabled']))
            self.assertEqual(self.connection_check.call_count, calls)
            self.assertEqual(self.client.parse_count, 1)
        finally:
            window.destroy()

    def test_reparse_window_cancel_preserves_task_and_makes_no_request(self):
        import tkinter as tk
        self.client.parse_failure = TimeoutError('offline timeout')
        report = self.run_sample()
        form = self.root / '.runtime' / 'desktop-form.json'
        form.write_text(json.dumps({'source': SOURCE, 'note': self.note}), encoding='utf-8')
        try:
            window = build_window(self.config_path, runner=lambda *a, **kw: self.fail('Must not run'))
        except tk.TclError as exc:
            self.skipTest(str(exc))
        try:
            window.withdraw()
            deadline=time.monotonic()+.4
            while time.monotonic()<deadline:
                window.update();time.sleep(.02)
            controls=window.desktop_controls
            self.assertTrue(controls['reparse'].instate(['!disabled']))
            with patch('tkinter.messagebox.askyesno', return_value=False) as confirm:
                controls['reparse'].invoke()
            confirm.assert_called_once()
            self.assertFalse(controls['state']['busy'])
            self.assertEqual(self.client.parse_count,1)
            folder=Path(report['report_path']).parent/'files'/report['id']
            self.assertTrue((folder/'download.zhiling-private.json').exists())
            self.assertEqual(list(folder.glob('download.zhiling-private.attempt-*.json')),[])
        finally:window.destroy()

    def test_same_name_failure_does_not_upload_and_shows_notice(self):
        with patch.object(self.uploader, 'check_destination', side_effect=NameConflict('原样提示')):
            result = self.run_sample()
        self.assertEqual(result['state'], 'name_conflict')
        self.assertEqual(self.uploader.calls, [])
        self.assertIn('原样提示', result_text(result))

    def test_other_pending_jobs_do_not_run(self):
        other = Queue(self.root / '.runtime' / 'desktop-jobs' / 'unrelated')
        self.addCleanup(other.db.close)
        ident = other.submit_note(SOURCE, '其他任务 #二创', self.config['categories'])
        self.assertEqual(self.run_sample()['state'], 'succeeded')
        self.assertEqual(other.get(ident)['state'], 'queued')
        self.assertEqual(len(self.uploader.calls), 1)

    def test_saved_environment_refusal_prevents_blind_retry_without_network(self):
        key = job_key(plan_request(self.config, SOURCE, self.note))
        queue = Queue(self.root / '.runtime' / 'desktop-jobs' / key)
        ident = queue.submit_note(SOURCE, self.note, self.config['categories'], key)
        queue.update(ident, 'destination_unchecked', error='old generic error')
        directory = queue.root / 'files' / ident / 'destination-checks' / 'fixture'
        directory.mkdir(parents=True)
        (directory / 'stdout.ndjson').write_text(json.dumps({'type':'result','action':'runtime','code':-104})+'\n')
        queue.db.close()
        result = self.run_sample(retry=True)
        self.assertEqual(result['state'], 'setup_required')
        self.assertTrue(result['integration_blocked'])
        self.assertIn('-104', result['error'])
        self.assertEqual(self.client.parse_count, 0)
        self.assertEqual(self.uploader.calls, [])

    def test_second_desktop_window_cannot_run_concurrently(self):
        root = self.root / '.runtime' / 'desktop-jobs'
        root.mkdir(parents=True)
        with WorkerLock(root), self.assertRaises(RuntimeError):
            self.run_sample()
        self.assertEqual(self.client.parse_count, 0)

    def test_invalid_category_stops_before_any_provider_call(self):
        self.note = '点评 #不存在'
        with self.assertRaises(ValueError):
            self.run_sample()
        self.assertEqual(self.client.parse_count, 0)
        self.assertEqual(self.uploader.calls, [])

    @unittest.skipUnless(os.name == 'nt', 'Windows desktop launch contract')
    def test_pythonw_preserves_managed_command_output(self):
        pythonw = Path(sys.executable).with_name('pythonw.exe')
        if not pythonw.exists():
            self.skipTest('pythonw unavailable')
        helper = self.root / 'offline-pythonw.py'
        result = self.root / 'pythonw-result.json'
        helper.write_text(
            'import sys, json\nfrom pathlib import Path\n'
            f'sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n'
            'from process_runner import run_managed\n'
            'r=run_managed([sys.executable,"-c","print(123456)"],timeout=10,capture_output=True)\n'
            f'Path({str(result)!r}).write_text(json.dumps([r.returncode,r.stdout,r.stderr]))\n', encoding='utf-8')
        subprocess.run([str(pythonw), str(helper)], timeout=20, check=True)
        self.assertEqual(json.loads(result.read_text())[:2], [0, '123456\r\n'])

    def test_window_requires_click_and_restores_form_without_autostart(self):
        import tkinter as tk
        calls = []

        def fake_run(path, text, note, **kwargs):
            calls.append((text, note))
            kwargs['progress']('模拟下载中')
            return {'state': 'succeeded', 'filename': '自动收藏窗口测试.mp4',
                    'category': '二创', 'full_path': '夸克网盘/离线测试/二创', 'notice': '模拟提示'}

        try:
            window = build_window(self.config_path, runner=fake_run)
        except tk.TclError as exc:
            self.skipTest(str(exc))
        try:
            window.withdraw()
            controls = window.desktop_controls
            window.update()
            self.assertEqual(calls, [])
            controls['source'].insert('1.0', SOURCE)
            controls['note'].insert('1.0', self.note)
            window.update()
            controls['start'].invoke()
            controls['start'].invoke()
            deadline = time.monotonic() + 5
            while controls['state']['busy'] and time.monotonic() < deadline:
                window.update()
                time.sleep(.02)
            self.assertFalse(controls['state']['busy'])
            self.assertEqual(calls, [(SOURCE, self.note)])
            self.assertEqual(controls['status'].get(), '已存入夸克')
            self.assertIn('模拟提示', controls['output'].get('1.0', 'end'))
        finally:
            window.destroy()
        second = build_window(self.config_path, runner=fake_run)
        try:
            second.withdraw()
            second.update()
            self.assertEqual(second.desktop_controls['source'].get('1.0', 'end-1c'), SOURCE)
            self.assertEqual(second.desktop_controls['note'].get('1.0', 'end-1c'), self.note)
            self.assertEqual(len(calls), 1)
        finally:
            second.destroy()


if __name__ == '__main__':
    unittest.main()
