"""Standalone upload recovery: temporary media and synthetic listings only."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import httpx

from collector import Queue, prepare, process_one, sha256
from quark_web import WebError
from quark_web_uploader import WebUploader
from upload_evidence import confirmed_upload, evidence_directory, review_token


class FakeWeb:
    def __init__(self):
        self.http = SimpleNamespace(close=lambda: None)
        self.rows = []
        self.auth_error = False
        self.missing_directory = False
        self.allow_upload = False
        self.fail_listing_after_upload = False
        self.upload_calls = 0
        self.list_calls = []
        self.resolve_calls = []

    def list_all(self, parent):
        self.list_calls.append(parent)
        if self.auth_error:
            raise WebError('登录已失效，请重新扫码。')
        if self.fail_listing_after_upload and self.upload_calls:
            raise WebError('模拟写入完成后核对连接中断。')
        return copy.deepcopy(self.rows) if parent == 'original-web-folder' else []

    def resolve(self, parts):
        self.resolve_calls.append(parts)
        if self.missing_directory:
            raise WebError('找不到目标目录。')
        return 'original-web-folder'

    def upload(self, path, parent, progress):
        if not self.allow_upload:
            raise AssertionError('Recovery must never upload')
        self.upload_calls += 1
        hashes = {key: hashlib.new(key, path.read_bytes()).hexdigest()
                  for key in ('md5', 'sha1', 'sha256')}
        self.rows = [{'fid': 'synthetic-fid', 'file_name': path.name,
                      'file': True, 'size': path.stat().st_size, **hashes}]
        return hashes


class QuarkRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'fixture.mp4'
        self.source.write_bytes(b'\0\0\0\x18ftypmp42' + b'offline' * 128)
        self.session = self.root / 'session.json'
        self.session.write_text('{"cookies":[]}', encoding='utf-8')
        self.queue = Queue(self.root / 'queue')
        self.addCleanup(self.queue.db.close)
        self.ident = self.queue.submit_note(str(self.source), '原点评 #二创', {'二创': 'configured-folder'})
        self.config = {'quark_web': {'session_file': str(self.session),
                        'category_paths': {'二创': ['搜集视频', '二创']}}}
        self.client = FakeWeb()
        self.uploader = WebUploader(self.config, client=self.client)
        self.addCleanup(self.uploader.close)
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'httpx.request'):
            guard = patch(target, side_effect=AssertionError('No external network in recovery tests'))
            guard.start(); self.addCleanup(guard.stop)

    def ambiguous_task(self, *, hashes=True):
        task = self.queue.claim()
        self.uploader.preflight()
        local = prepare(task, self.queue.root, self.config)
        self.uploader.check_destination(local, task)
        task = self.queue.start_upload(task, local, sha256(local))
        self.queue.update(self.ident, 'needs_review', error='Synthetic lost upload response')
        self.task = self.queue.get(self.ident)
        self.local = local
        remote = {'fid': 'original-fid', 'file_name': local.name, 'file': True,
                  'size': local.stat().st_size, 'updated_at': 123456}
        if hashes:
            remote.update({key: hashlib.new(key, local.read_bytes()).hexdigest() for key in ('md5', 'sha1')})
        self.client.rows = [remote]
        self.client.list_calls.clear(); self.client.resolve_calls.clear()
        return self.task

    def check(self, confirmation=None):
        result = self.uploader.review(self.local, self.queue.get(self.ident), confirmation=confirmation)
        self.assertEqual(self.client.upload_calls, 0)
        return result

    def test_lost_receipt_hash_match_recovers_without_upload_and_survives_restart(self):
        self.ambiguous_task()
        result = self.check()
        self.assertEqual(result['state'], 'succeeded')
        self.assertIn('MD5', result['notice'])
        self.assertEqual(self.client.list_calls, ['original-web-folder'])
        self.assertEqual(self.client.resolve_calls, [])
        self.assertEqual(confirmed_upload(self.queue.root, self.task)['remote_fid'], 'original-fid')
        self.assertEqual(confirmed_upload(self.queue.root, self.task)['full_path'], '夸克网盘/搜集视频/二创')
        self.queue.recover()
        self.assertEqual(self.queue.get(self.ident)['state'], 'succeeded')
        self.assertEqual(self.queue.get(self.ident)['input_text'], '原点评 #二创')

    def test_missing_hash_requires_explicit_confirmation_of_same_candidate(self):
        self.ambiguous_task(hashes=False)
        first = self.check()
        self.assertEqual(first['state'], 'needs_review')
        self.assertEqual(first['review_candidate']['filename'], self.local.name)
        self.assertEqual(first['review_candidate']['full_path'], '夸克网盘/搜集视频/二创/' + self.local.name)
        with self.assertRaises(ValueError): confirmed_upload(self.queue.root, self.task)
        result = self.check(first['review_confirmation'])
        self.assertEqual(result['state'], 'succeeded')
        self.assertIn('明确确认', result['notice'])
        self.assertIn('未冒充自动校验', result['notice'])
        self.queue.review_upload(self.ident)
        self.assertEqual(self.queue.get(self.ident)['state'], 'succeeded')

    def test_changed_candidate_does_not_accept_previous_confirmation(self):
        self.ambiguous_task(hashes=False)
        first = self.check()
        self.client.rows[0]['fid'] = 'different-fid'
        result = self.check(first['review_confirmation'])
        self.assertEqual(result['state'], 'needs_review')
        self.assertNotEqual(result['review_confirmation'], first['review_confirmation'])
        self.assertIn('已变化', result['error'])
        self.assertEqual(self.queue.get(self.ident)['state'], 'needs_review')

    def test_changed_remote_revision_invalidates_confirmation_even_with_same_size(self):
        self.ambiguous_task(hashes=False)
        first = self.check()
        self.client.rows[0]['updated_at'] += 1
        self.assertEqual(self.check(first['review_confirmation'])['state'], 'needs_review')

    def test_confirmation_must_have_been_displayed_not_only_computeable(self):
        self.ambiguous_task(hashes=False)
        first = self.check()
        candidate = evidence_directory(self.queue.root, self.task) / 'web-review-candidate.json'
        candidate.unlink()
        self.assertEqual(self.check(first['review_confirmation'])['state'], 'needs_review')

    def test_empty_listing_never_means_safe_to_retry(self):
        self.ambiguous_task()
        self.client.rows = []
        result = self.check()
        self.assertEqual(result['state'], 'needs_review')
        self.assertNotIn('review_confirmation', result)
        with self.assertRaises(ValueError): self.queue.retry(self.ident)
        self.assertIsNone(process_one(self.queue, self.config, self.uploader))
        self.assertEqual(self.client.upload_calls, 0)

    def test_duplicates_and_wrong_type_or_size_cannot_be_confirmed(self):
        self.ambiguous_task(hashes=False)
        original = copy.deepcopy(self.client.rows[0])
        cases = [[original, dict(original, fid='other')], [dict(original, size=original['size'] + 1)],
                 [dict(original, file=False)], [dict(original, size=True)],
                 [dict(original, file_name=original['file_name'].upper())]]
        for rows in cases:
            with self.subTest(rows=rows):
                self.client.rows = rows
                result = self.check('any-token')
                self.assertEqual(result['state'], 'needs_review')
                self.assertNotIn('review_confirmation', result)

    def test_hash_mismatch_never_accepts_manual_confirmation_or_same_size(self):
        self.ambiguous_task(hashes=False)
        first = self.check()
        self.client.rows[0]['md5'] = '0' * 32
        result = self.check(first['review_confirmation'])
        self.assertEqual(result['state'], 'needs_review')
        self.assertNotIn('review_confirmation', result)
        self.assertIn('不匹配', result['error'])

    def test_one_matching_hash_is_not_automatic_content_confirmation(self):
        self.ambiguous_task()
        del self.client.rows[0]['sha1']
        self.assertEqual(self.check()['state'], 'needs_review')

    def test_local_same_size_corruption_stops_before_network_read(self):
        self.ambiguous_task()
        payload = self.local.read_bytes()
        self.local.write_bytes(payload[:-1] + b'!')
        result = self.check()
        self.assertEqual(result['state'], 'needs_review')
        self.assertEqual(self.client.list_calls, [])

    def test_review_uses_original_folder_even_after_config_changes(self):
        self.ambiguous_task()
        self.config['quark_web']['category_paths']['二创'] = ['New folder', '二创']
        result = self.check()
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(self.client.list_calls, ['original-web-folder'])
        self.assertEqual(self.client.resolve_calls, [])
        proof = json.loads((evidence_directory(self.queue.root, self.task) / 'web-recovery.json').read_text(encoding='utf-8'))
        self.assertEqual(proof['full_path'], '夸克网盘/搜集视频/二创')

    def test_review_auth_failure_remains_pending_then_recovers_after_login(self):
        self.ambiguous_task()
        self.client.auth_error = True
        result = self.check()
        self.assertEqual(result['state'], 'needs_review')
        self.assertIn('登录', result['error'])
        self.client.auth_error = False
        self.assertEqual(self.check()['state'], 'succeeded')

    def test_wrong_state_cannot_start_remote_review(self):
        task = self.queue.get(self.ident)
        with self.assertRaises(ValueError): self.uploader.review(self.source, task)
        self.assertEqual(self.client.list_calls, [])

    def test_recovery_evidence_cannot_be_rebound_or_downgraded(self):
        self.ambiguous_task()
        self.check()
        path = evidence_directory(self.queue.root, self.task) / 'web-recovery.json'
        original = json.loads(path.read_text(encoding='utf-8'))
        mutations = [lambda p: p['candidate'].update(attempt='0' * 32),
                     lambda p: p['candidate']['remote'].update(size=1),
                     lambda p: p['candidate']['binding'].update(web_parent='other-folder'),
                     lambda p: p.update(method='user_confirmed', user_confirmed=True),
                     lambda p: p['candidate']['local_hashes'].update(md5='0' * 32)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                changed = copy.deepcopy(original)
                mutate(changed)
                changed['candidate_token'] = review_token(changed['candidate'])
                path.write_text(json.dumps(changed), encoding='utf-8')
                with self.assertRaises(ValueError): confirmed_upload(self.queue.root, self.task)
        path.write_text(json.dumps(original), encoding='utf-8')

    def test_valid_old_receipt_is_not_hidden_by_broken_new_recovery_file(self):
        self.client.allow_upload = True
        completed = process_one(self.queue, self.config, self.uploader)
        self.assertEqual(completed['state'], 'succeeded')
        directory = evidence_directory(self.queue.root, completed)
        (directory / 'web-recovery.json').write_text('{broken', encoding='utf-8')
        self.assertEqual(confirmed_upload(self.queue.root, completed)['remote_fid'], 'synthetic-fid')

    def test_actual_upload_then_listing_failure_recovers_with_read_only_review(self):
        self.client.allow_upload = True
        self.client.fail_listing_after_upload = True
        result = process_one(self.queue, self.config, self.uploader)
        self.assertEqual(result['state'], 'needs_review')
        self.assertEqual(self.client.upload_calls, 1)
        directory = evidence_directory(self.queue.root, result)
        self.assertTrue((directory / 'web-error.json').is_file())
        self.assertFalse((directory / 'web-upload.json').exists())
        with self.assertRaises(ValueError): self.queue.retry(self.ident)
        self.client.allow_upload = False
        self.client.fail_listing_after_upload = False
        self.client.list_calls.clear(); self.client.resolve_calls.clear()
        recovered = self.uploader.review(Path(result['local_file']), result)
        self.assertEqual(recovered['state'], 'succeeded')
        self.assertEqual(self.client.upload_calls, 1)
        self.assertEqual(self.client.list_calls, ['original-web-folder'])
        self.assertEqual(self.client.resolve_calls, [])
        self.queue.review_upload(self.ident)
        self.assertEqual(self.queue.get(self.ident)['state'], 'succeeded')

    def test_invalid_old_receipt_can_be_recovered_with_new_strong_evidence(self):
        self.ambiguous_task()
        directory = evidence_directory(self.queue.root, self.task)
        (directory / 'web-upload.json').write_text('{broken', encoding='utf-8')
        self.assertEqual(self.check()['state'], 'succeeded')
        self.assertEqual(confirmed_upload(self.queue.root, self.task)['full_path'], '夸克网盘/搜集视频/二创')
        self.assertEqual((directory / 'web-upload.json').read_text(), '{broken')

    def test_old_receipt_path_must_match_its_bound_directory(self):
        self.client.allow_upload = True
        task = process_one(self.queue, self.config, self.uploader)
        directory = evidence_directory(self.queue.root, task)
        path = directory / 'web-upload.json'
        proof = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(confirmed_upload(self.queue.root, task)['full_path'], '夸克网盘/搜集视频/二创')
        proof['full_path'] = '夸克网盘/错误位置'
        path.write_text(json.dumps(proof), encoding='utf-8')
        with self.assertRaises(ValueError): confirmed_upload(self.queue.root, task)
        # Older receipts without path fields keep their successful upload
        # evidence but must not supply an unbound display path.
        del proof['binding']['path']
        path.write_text(json.dumps(proof), encoding='utf-8')
        self.assertEqual(confirmed_upload(self.queue.root, task)['remote_fid'], 'synthetic-fid')
        self.assertNotIn('full_path', confirmed_upload(self.queue.root, task))

    def test_valid_recovery_supplies_path_when_old_receipt_has_wrong_display_path(self):
        self.client.allow_upload = True
        task = process_one(self.queue, self.config, self.uploader)
        directory = evidence_directory(self.queue.root, task)
        path = directory / 'web-upload.json'
        proof = json.loads(path.read_text(encoding='utf-8'))
        proof['full_path'] = '夸克网盘/错误位置'
        path.write_text(json.dumps(proof), encoding='utf-8')
        self.queue.update(self.ident, 'needs_review')
        task = self.queue.get(self.ident)
        recovered = self.uploader.review(Path(task['local_file']), task)
        self.assertEqual(recovered['state'], 'succeeded')
        self.assertEqual(confirmed_upload(self.queue.root, task)['full_path'], '夸克网盘/搜集视频/二创')
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['full_path'], '夸克网盘/错误位置')
        self.assertEqual(self.client.upload_calls, 1)

    def test_legacy_display_path_comes_only_from_validated_final_receipt(self):
        from test_upload_evidence import receipt_rows, write_receipt
        self.ambiguous_task()
        rows = receipt_rows(self.task)
        rows[-1]['data']['fullPath'] = '夸克网盘/原分类/原点评.mp4'
        write_receipt(self.queue.root, self.task, rows)
        self.assertEqual(confirmed_upload(self.queue.root, self.task)['full_path'], '夸克网盘/原分类/原点评.mp4')
        rows[-1]['data']['fullPath'] = 123
        write_receipt(self.queue.root, self.task, rows)
        self.assertNotIn('full_path', confirmed_upload(self.queue.root, self.task))
        rows[-1]['data']['fullPath'] = '夸克网盘/未经验证'
        rows[-1]['data']['totalSize'] += 1
        write_receipt(self.queue.root, self.task, rows)
        with self.assertRaises(ValueError): confirmed_upload(self.queue.root, self.task)

    def test_close_failure_does_not_replace_task_result_and_is_idempotent(self):
        self.client.allow_upload = True
        with patch.object(self.client.http, 'close', side_effect=RuntimeError('Synthetic close failure')) as close:
            def run():
                try:
                    return process_one(self.queue, self.config, self.uploader)
                finally:
                    self.uploader.close()
            result = run()
            self.assertEqual(result['state'], 'succeeded')
            self.uploader.close()
            close.assert_called_once()

    def test_corrupt_session_becomes_setup_required_and_resumes_original_task(self):
        self.session.write_text('{broken', encoding='utf-8')
        http = httpx.Client(transport=httpx.MockTransport(lambda r: self.fail('No requests')))
        self.addCleanup(http.close)
        with patch('quark_web.http_client', return_value=http):
            uploader = WebUploader(self.config)
            try:
                result = process_one(self.queue, self.config, uploader)
            finally:
                uploader.close()
        self.assertEqual(result['state'], 'setup_required')
        self.assertEqual(result['input_text'], '原点评 #二创')
        self.session.write_text('{"cookies":[]}', encoding='utf-8')
        self.client.allow_upload = True
        self.queue.retry(self.ident)
        result = process_one(self.queue, self.config, self.uploader)
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['id'], self.ident)
        self.assertEqual(self.client.upload_calls, 1)

    def test_missing_directory_and_login_recovery_reuse_prepared_file(self):
        self.client.missing_directory = True
        result = process_one(self.queue, self.config, self.uploader)
        self.assertEqual(result['state'], 'destination_unchecked')
        self.source.unlink()
        self.queue.retry(self.ident)
        self.client.auth_error = True
        self.assertEqual(process_one(self.queue, self.config, self.uploader)['state'], 'setup_required')
        self.client.auth_error = self.client.missing_directory = False
        self.client.allow_upload = True
        self.queue.retry(self.ident)
        with patch('collector.run_managed', side_effect=AssertionError('Must reuse original media')):
            result = process_one(self.queue, self.config, self.uploader)
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['id'], self.ident)
        self.assertEqual(self.client.upload_calls, 1)

    def test_malformed_login_cookie_fields_remain_resumable(self):
        for payload in ('{"cookies":[{"domain":null}]}', '{"cookies":[{"domain":1}]}', '{"cookies":true}'):
            with self.subTest(payload=payload):
                self.session.write_text(payload, encoding='utf-8')
                http = httpx.Client(transport=httpx.MockTransport(lambda r: self.fail('No requests')))
                with patch('quark_web.http_client', return_value=http):
                    uploader = WebUploader(self.config)
                    try:
                        result = process_one(self.queue, self.config, uploader)
                    finally:
                        uploader.close(); http.close()
                self.assertEqual(result['state'], 'setup_required')
                self.assertEqual(result['id'], self.ident)
                self.queue.retry(self.ident)

    def test_invalid_session_file_setting_does_not_crash_task(self):
        for value in (None, 123, {}):
            with self.subTest(value=value):
                self.config['quark_web']['session_file'] = value
                uploader = WebUploader(self.config)
                try:
                    result = process_one(self.queue, self.config, uploader)
                finally:
                    uploader.close()
                self.assertEqual(result['state'], 'setup_required')
                self.queue.retry(self.ident)


if __name__ == '__main__':
    unittest.main()
