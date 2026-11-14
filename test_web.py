import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from collector import Queue, process_one
from test_collector import FakeUploader
from web_app import create_app
from worker import run


class WebTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / 'config.json'
        self.config = {'data_dir': 'data', 'categories': {'二创': 'private-folder-id'},
                       'dtk': {'base_url': 'http://127.0.0.1:8000'}}
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')
        self.token = 'test-only-random-access-token-123456789'
        self.app = create_app(self.config_path, self.token, 'http://localhost')
        self.client = TestClient(self.app, base_url='http://localhost')
        self.headers = {'X-Collector-Request': '1'}
        self.ready = {'platforms': {'douyin': True, 'wechat': False}, 'quark_ready': True, 'worker_online': True}
        self.mock_ready = patch('web_app.readiness', return_value=self.ready)
        self.mock_ready.start()
        self.login()

    def tearDown(self):
        self.mock_ready.stop()
        self.client.close()
        self.temp.cleanup()

    def login(self):
        response = self.client.post('/api/login', json={'token': self.token}, headers=self.headers)
        self.assertEqual(response.status_code, 200)

    def create(self, text='视频分享 #影视 https://v.douyin.com/test/ #话题', draft_id=None):
        result = self.client.post('/api/drafts', json={'id': draft_id or str(uuid4()), 'text': text}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def submit(self, draft, note='这是一个测试 #二创'):
        current = self.client.get('/api/drafts/' + draft['id']).json()
        return self.client.post('/api/drafts/' + draft['id'] + '/submit', json={'note': note, 'version': current['version']}, headers=self.headers)

    def test_auth_csrf_and_no_secret_in_responses(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/drafts').status_code, 401)
        self.assertEqual(self.client.post('/api/login', json={'token': self.token}).status_code, 403)
        response = self.client.post('/api/login', json={'token': self.token}, headers=dict(self.headers, Origin='https://evil.example'))
        self.assertEqual(response.status_code, 403)
        self.login()
        result = self.client.get('/api/status')
        self.assertNotIn('private-folder-id', result.text)
        self.assertNotIn(self.token, result.text)
        self.assertEqual(result.headers['cache-control'], 'no-store')
        self.assertEqual(self.client.get('/api/status', headers={'host': 'evil.example'}).status_code, 400)

    def test_https_required_and_secure_cookie(self):
        with self.assertRaises(RuntimeError):
            create_app(self.config_path, self.token, 'http://public.example')
        secure = TestClient(create_app(self.config_path, self.token, 'https://collector.example'), base_url='https://collector.example')
        with secure:
            result = secure.post('/api/login', json={'token': self.token}, headers=self.headers)
            cookie = result.headers['set-cookie'].lower()
            self.assertIn('secure', cookie)
            self.assertIn('httponly', cookie)
            self.assertIn('samesite=strict', cookie)

    def test_two_videos_keep_their_own_notes_and_caption_tags_ignored(self):
        first = self.create()
        second = self.create('https://v.douyin.com/second/')
        self.assertEqual(self.submit(second, '第二个点评 #二创').status_code, 200)
        self.assertEqual(self.submit(first).status_code, 200)
        queue = Queue(self.root / 'data')
        try:
            tasks = {t['source']: t for t in queue.all()}
            self.assertEqual(tasks[first['source']]['comment'], '这是一个测试')
            self.assertEqual(tasks[second['source']]['comment'], '第二个点评')
            self.assertEqual(tasks[first['source']]['parent_fid'], 'private-folder-id')
        finally:
            queue.db.close()

    def test_invalid_note_is_retained_but_never_queued(self):
        draft = self.create()
        for note in ['点评没标签', '点评 #不存在', '点评 #二创 #二创', '#二创']:
            result = self.submit(draft, note)
            self.assertEqual(result.status_code, 400)
            saved = self.client.get('/api/drafts/' + draft['id']).json()
            self.assertEqual(saved['note'], note)
            self.assertEqual(saved['state'], 'awaiting_note')

    def test_stale_page_cannot_overwrite_or_submit_over_a_newer_note(self):
        draft = self.create()
        path = '/api/drafts/' + draft['id']
        saved = self.client.patch(path, json={'note': '新点评 #二创', 'version': 0}, headers=self.headers)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()['version'], 1)
        for method, route in ((self.client.patch, path), (self.client.post, path + '/submit')):
            response = method(route, json={'note': '旧页面点评 #二创', 'version': 0}, headers=self.headers)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()['code'], 'draft_conflict')
            self.assertEqual(response.json()['current']['note'], '新点评 #二创')
        current = self.client.get(path).json()
        self.assertEqual(current['note'], '新点评 #二创')
        self.assertIsNone(current['task_id'])
        self.assertEqual(self.client.patch(path, json={'note': '新点评 #二创', 'version': 0}, headers=self.headers).json()['version'], 1)
        for version in (None, -1, True, '1'):
            payload = {'note': '不能覆盖'} if version is None else {'note': '不能覆盖', 'version': version}
            self.assertEqual(self.client.patch(path, json=payload, headers=self.headers).status_code, 422)
        chosen = self.client.post(path + '/submit', json={'note': '选择我这份 #二创', 'version': 1}, headers=self.headers)
        self.assertEqual(chosen.status_code, 200)
        self.assertEqual(chosen.json()['note'], '选择我这份 #二创')

    def test_concurrent_different_notes_have_one_winner_and_one_conflict(self):
        draft = self.create()
        def send(note):
            with TestClient(self.app, base_url='http://localhost') as client:
                client.cookies.update(self.client.cookies)
                return client.patch('/api/drafts/' + draft['id'], json={'note': note, 'version': 0}, headers=self.headers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(send, ['第一份 #二创', '第二份 #二创']))
        self.assertEqual(sorted(r.status_code for r in results), [200, 409])
        winner = next(r.json() for r in results if r.status_code == 200)
        self.assertEqual(self.client.get('/api/drafts/' + draft['id']).json()['note'], winner['note'])

    def test_edit_between_save_and_enqueue_cannot_be_bypassed(self):
        from web_store import DraftStore
        draft = self.create()
        def intervene(*args):
            data = DraftStore(self.root / 'data')
            try:
                current = data.get(draft['id'])
                data.save_note(draft['id'], '检查期间另一页的新点评 #二创', current['version'])
            finally:
                data.close()
            return self.ready
        with patch('web_app.readiness', side_effect=intervene):
            result = self.submit(draft)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()['code'], 'draft_conflict')
        current = self.client.get('/api/drafts/' + draft['id']).json()
        self.assertIsNone(current['task_id'])
        self.assertEqual(current['note'], '检查期间另一页的新点评 #二创')

    def test_existing_draft_migration_preserves_original_note(self):
        import sqlite3
        from contextlib import closing
        from web_store import DraftStore
        root = self.root / 'old-data'
        root.mkdir()
        with closing(sqlite3.connect(root / 'tasks.sqlite')) as db:
            db.execute('''CREATE TABLE drafts (id TEXT PRIMARY KEY,source TEXT,share_text TEXT,
                platform TEXT,note TEXT,created REAL)''')
            db.execute('INSERT INTO drafts VALUES (?,?,?,?,?,?)',
                       ('legacy', 'https://v.douyin.com/old/', '旧分享', 'douyin', '原有点评 #二创', 1))
            db.commit()
        data = DraftStore(root)
        try:
            self.assertEqual(data.get('legacy')['note'], '原有点评 #二创')
            self.assertEqual(data.get('legacy')['version'], 0)
        finally:
            data.close()

    def test_preview_matches_queued_name_and_file_sent_to_uploader(self):
        cases = [
            ('这是一个测试', '这是一个测试.mp4', False),
            ('CON', '_CON.mp4', True),
            (' 开头\n讲述?结尾 . ', '开头 讲述 结尾.mp4', True),
            ('中' * 121, '中' * 80 + '.mp4', True),
            ('😀' * 100, '😀' * 60 + '.mp4', True),
            ('a' * 119 + '😀x', 'a' * 119 + '.mp4', True),
            ('Z' * 121, 'Z' * 120 + '.mp4', True),
        ]
        queue = Queue(self.root / 'data')
        try:
            for comment, filename, adjusted in cases:
                with self.subTest(comment=comment):
                    draft = self.create()
                    note = comment + ' #二创'
                    before = len(queue.all())
                    response = self.client.post('/api/preview', json={'note': note}, headers=self.headers)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json(), {'category': '二创', 'filename': filename, 'adjusted': adjusted})
                    self.assertEqual(len(queue.all()), before, 'preview must not create tasks')
                    self.assertEqual(self.client.get('/api/drafts/' + draft['id']).json()['note'], '')
                    submitted = self.submit(draft, note)
                    self.assertEqual(submitted.status_code, 200, submitted.text)
                    self.assertEqual(submitted.json()['filename'], filename)
                    uploader = FakeUploader()
                    def download(args, **kwargs):
                        Path(args[-1]).write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'x' * 128)
                    config = {'download_commands': {'douyin': [sys.executable, 'offline-only', '{output}']}}
                    with patch('collector.run_managed', side_effect=download):
                        result = process_one(queue, config, uploader)
                    self.assertEqual(result['state'], 'succeeded')
                    self.assertEqual(uploader.calls, [(filename, 'private-folder-id')])
                    self.assertEqual(queue.get(submitted.json()['task_id'])['comment'], comment.strip())
        finally:
            queue.db.close()

    def test_preview_invalid_notes_are_rejected_without_side_effects(self):
        for note in ('无类目', '#二创', '点评 #不存在', '点评 #二创 #二创', '<>:?* #二创', '\ud800 #二创'):
            with self.subTest(note=repr(note)):
                response = self.client.post('/api/preview', content=json.dumps({'note': note}),
                                            headers={**self.headers, 'Content-Type': 'application/json'})
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.client.get('/api/drafts').json()['items'], [])

    def test_preview_is_authenticated_and_has_no_private_configuration(self):
        result = self.client.post('/api/preview', json={'note': '点评 #二创'}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertNotIn('private-folder-id', result.text)
        self.assertEqual(result.headers['cache-control'], 'no-store')
        self.client.cookies.clear()
        self.assertEqual(self.client.post('/api/preview', json={'note': '点评 #二创'}, headers=self.headers).status_code, 401)

    def test_existing_upload_keeps_actual_name_after_naming_rule_change(self):
        draft = self.create()
        submitted = self.submit(draft, '中' * 121 + ' #二创').json()
        queue = Queue(self.root / 'data')
        old_name = '中' * 120 + '.mp4'
        try:
            for prefix in ('C:\\private\\old-machine\\', '/private/old-machine/'):
                queue.update(submitted['task_id'], 'succeeded', local_file=prefix + old_name,
                             remote_fid='offline-test-only')
                result = self.client.get('/api/drafts/' + draft['id'])
                self.assertEqual(result.json()['filename'], old_name)
                self.assertNotIn('old-machine', result.text)
                self.assertNotIn('offline-test-only', result.text)
        finally:
            queue.db.close()

    def test_unconfigured_channels_is_draft_only_and_never_fetched(self):
        draft = self.create('https://weixin.qq.com/sph/test-only/')
        result = self.submit(draft)
        self.assertEqual(result.status_code, 409)
        self.assertIn('点评已保存', result.json()['detail'])
        self.assertEqual(self.client.get('/api/drafts/' + draft['id']).json()['state'], 'awaiting_note')

    def test_missing_downloader_is_detected_before_enqueue_and_note_is_saved(self):
        self.mock_ready.stop()
        self.config['dtk'] = {}
        self.config['download_commands'] = {'douyin': [str(self.root / 'missing-python'), '{source}', '{output}']}
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')
        draft = self.create()
        result = self.submit(draft)
        self.assertEqual(result.status_code, 409)
        self.assertIn('下载程序未安装', result.json()['detail'])
        self.assertNotIn(str(self.root), result.text)
        saved = self.client.get('/api/drafts/' + draft['id']).json()
        self.assertEqual(saved['note'], '这是一个测试 #二创')
        self.assertIsNone(saved['task_id'])
        self.assertFalse(self.client.get('/api/status').json()['platforms']['douyin'])

    def test_local_files_unrelated_links_and_multiple_links_rejected(self):
        for text in [r'C:\private\video.mp4', 'http://127.0.0.1/private', 'https://v.douyin.com/a/ https://v.douyin.com/b/']:
            result = self.client.post('/api/drafts', json={'id': str(uuid4()), 'text': text}, headers=self.headers)
            self.assertEqual(result.status_code, 400)

    def test_repeated_request_survives_reopen_and_cannot_change_content(self):
        key = str(uuid4())
        draft = self.create(draft_id=key)
        self.assertEqual(self.create(draft_id=key)['id'], draft['id'])
        first = self.submit(draft).json()
        self.ready['worker_online'] = False
        self.assertEqual(self.submit(draft).json()['task_id'], first['task_id'])
        self.assertEqual(self.submit(draft, '改点评 #二创').status_code, 400)
        other = TestClient(create_app(self.config_path, self.token, 'http://localhost'), base_url='http://localhost')
        with other:
            other.cookies.update(self.client.cookies)
            self.assertEqual(other.get('/api/drafts/' + key).json()['task_id'], first['task_id'])

    def test_concurrent_submit_creates_one_task(self):
        draft = self.create()
        def send(_):
            with TestClient(self.app, base_url='http://localhost') as client:
                client.cookies.update(self.client.cookies)
                return client.post('/api/drafts/' + draft['id'] + '/submit', json={'note': '这是一个测试 #二创', 'version': draft['version']}, headers=self.headers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(send, range(4)))
        self.assertTrue(all(r.status_code == 200 for r in results), [(r.status_code, r.text) for r in results])
        self.assertEqual(len({r.json()['task_id'] for r in results}), 1)

    def test_unknown_upload_cannot_be_retried(self):
        draft = self.create()
        task = self.submit(draft).json()
        queue = Queue(self.root / 'data')
        queue.update(task['task_id'], 'needs_review', error='private diagnostic with token')
        queue.db.close()
        result = self.client.post('/api/drafts/' + draft['id'] + '/retry', json={}, headers=self.headers)
        self.assertEqual(result.status_code, 400)
        self.assertNotIn('private diagnostic', self.client.get('/api/drafts').text)

    def test_cancelled_draft_rejects_late_save_and_submit_but_retains_text(self):
        draft = self.create()
        path = '/api/drafts/' + draft['id']
        self.client.patch(path, json={'note': '原点评 #二创', 'version': 0}, headers=self.headers)
        result = self.client.post(path + '/cancel', json={}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['state'], 'cancelled')
        self.assertEqual(result.json()['note'], '原点评 #二创')
        self.assertEqual(self.client.post(path + '/cancel', json={}, headers=self.headers).json(), result.json())
        self.assertEqual(self.client.patch(path, json={'note': '迟到的修改 #二创', 'version': 1}, headers=self.headers).status_code, 400)
        self.assertEqual(self.submit(draft).status_code, 400)
        self.assertIsNone(self.client.get(path).json()['task_id'])

    def test_cancel_between_note_save_and_enqueue_does_not_create_task(self):
        from web_store import DraftStore
        draft = self.create()
        def intervene(*args):
            data = DraftStore(self.root / 'data')
            try:
                data.cancel(draft['id'])
            finally:
                data.close()
            return self.ready
        with patch('web_app.readiness', side_effect=intervene):
            self.assertEqual(self.submit(draft).status_code, 400)
        current = self.client.get('/api/drafts/' + draft['id']).json()
        self.assertEqual(current['state'], 'cancelled')
        self.assertIsNone(current['task_id'])

    def test_task_cancel_is_authenticated_and_respects_upload_boundary(self):
        draft = self.create()
        task = self.submit(draft).json()
        path = '/api/drafts/' + draft['id'] + '/cancel'
        self.assertEqual(self.client.post(path, json={}, headers=self.headers).json()['state'], 'cancelled')
        self.assertEqual(self.submit(draft).json()['state'], 'cancelled')
        queue = Queue(self.root / 'data')
        try:
            queue.update(task['task_id'], 'uploading')
            result = self.client.post(path, json={}, headers=self.headers)
            self.assertEqual(result.status_code, 409)
            self.assertEqual(result.json()['current']['state'], 'uploading')
            self.assertNotIn('private-folder-id', result.text)
            self.client.cookies.clear()
            self.assertEqual(self.client.post(path, json={}, headers=self.headers).status_code, 401)
            self.assertEqual(queue.get(task['task_id'])['state'], 'uploading')
        finally:
            queue.db.close()

    def test_revision_endpoint_copies_note_protects_history_and_requires_auth(self):
        draft=self.create(); task=self.submit(draft).json()
        path='/api/drafts/'+draft['id']+'/revise'
        queue=Queue(self.root/'data')
        try:
            self.assertEqual(self.client.post(path,json={},headers=self.headers).status_code,409)
            queue.update(task['task_id'],'name_conflict')
            response=self.client.post(path,json={},headers=self.headers)
            self.assertEqual(response.status_code,200)
            child=response.json()['draft']
            self.assertEqual(child['source'],draft['source'])
            self.assertEqual(child['note'],'这是一个测试 #二创')
            self.assertEqual(response.json()['original']['state'],'superseded')
            self.assertNotIn('private-folder-id',response.text)
            self.assertEqual(self.client.post(path,json={},headers=self.headers).json()['draft']['id'],child['id'])
            updated=self.submit(child,'修改后的点评 #二创')
            self.assertEqual(updated.status_code,200)
            self.assertEqual(queue.get(updated.json()['task_id'])['prepared_from'],task['task_id'])
            self.client.cookies.clear()
            self.assertEqual(self.client.post(path,json={},headers=self.headers).status_code,401)
        finally:queue.db.close()

    def test_review_endpoint_recovers_receipt_without_worker_or_platform_access(self):
        from test_upload_evidence import write_receipt
        from collector import sha256
        draft = self.create()
        self.submit(draft)
        queue = Queue(self.root / 'data')
        try:
            task = queue.claim()
            folder = queue.root / 'files' / task['id']
            folder.mkdir(parents=True)
            local = folder / '这是一个测试.mp4'
            local.write_bytes(b'offline-receipt-fixture')
            task = queue.start_upload(task, local, sha256(local))
            write_receipt(queue.root, task)
            path = '/api/drafts/' + draft['id'] + '/review'
            # The active upload cannot be confirmed while it is still running.
            self.assertEqual(self.client.post(path, json={}, headers=self.headers).status_code, 400)
            queue.update(task['id'], 'needs_review')
            self.ready.update(worker_online=False, quark_ready=False)
            with patch('collector.run_managed') as launch:
                result = self.client.post(path, json={}, headers=self.headers)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()['state'], 'succeeded')
                self.assertIn('没有重复上传', result.json()['review_note'])
                for secret in ('offline-fid', 'private-folder-id', 'upload_snapshot', 'stdout.ndjson', str(self.root)):
                    self.assertNotIn(secret, result.text)
                self.assertEqual(self.client.post(path, json={}, headers=self.headers).json()['state'], 'succeeded')
                launch.assert_not_called()
            self.assertEqual(queue.db.execute('SELECT COUNT(*) FROM upload_reviews').fetchone()[0], 1)
            self.client.cookies.clear()
            self.assertEqual(self.client.post(path, json={}, headers=self.headers).status_code, 401)
        finally:
            queue.db.close()

    def test_worker_finishes_after_web_client_closed_using_test_uploader(self):
        # All network effects are substituted; this is an integration test, not Quark evidence.
        draft = self.create()
        self.submit(draft)
        self.client.close()
        fixture = self.root / 'fixture.mp4'
        fixture.write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'0' * 100)
        self.config['dtk'] = {}
        self.config['download_command'] = [sys.executable, '-c', 'import shutil,sys;shutil.copyfile(sys.argv[1],sys.argv[2])', str(fixture), '{output}']
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')
        stopped = threading.Event()
        uploader = FakeUploader()
        def once(queue, config):
            result = process_one(queue, config, uploader)
            stopped.set()
            return result
        run(self.config_path, stopped, process=once)
        queue = Queue(self.root / 'data')
        try:
            self.assertEqual(queue.all()[0]['state'], 'succeeded')
            self.assertEqual(uploader.calls, [('这是一个测试.mp4', 'private-folder-id')])
        finally:
            queue.db.close()
        self.assertFalse((self.root / 'data/worker-heartbeat.json').exists())

    def test_static_page_share_route_and_manifest(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/share?text=hello').status_code, 200)
        module = self.client.get('/assets/preview.mjs')
        self.assertEqual(module.status_code, 200)
        self.assertIn('javascript', module.headers['content-type'])
        manifest = self.client.get('/assets/manifest.webmanifest').json()
        self.assertEqual(manifest['share_target']['action'], '/share')
        self.assertEqual(self.client.get('/api/drafts').json()['items'], [])


if __name__ == '__main__':
    unittest.main()
