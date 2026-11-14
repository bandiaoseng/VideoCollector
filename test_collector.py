import json
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch

from collector import Queue, WorkerLock, parse_upload, prepare, process_one, video_name
from share_input import parse_share
from note_input import parse_note


class FakeUploader:
    """Offline test double, never represents an actual cloud upload."""
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def preflight(self):
        pass

    def check_destination(self, path, task, cancel_check=None):
        return ''

    def upload(self, path, task):
        self.calls.append((path.name, task['parent_fid']))
        if self.fail:
            raise TimeoutError('simulated connection loss after server commit')
        return {'state': 'succeeded', 'remote_fid': 'test-only-fid', 'notice': '', 'error': None}


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.queue = Queue(self.root / 'data')
        self.source = self.root / 'original.mp4'
        self.source.write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 128)

    def tearDown(self):
        self.queue.db.close()
        self.temp.cleanup()

    def submit(self, **kwargs):
        return self.queue.submit(str(self.source), kwargs.pop('comment', '先讲痛点，再讲方案'),
                                 '营销', {'营销': 'folder-123'}, **kwargs)

    def test_named_copy_and_category_reach_uploader(self):
        original = self.source.read_bytes()
        task_id = self.submit()
        uploader = FakeUploader()
        result = process_one(self.queue, {}, uploader)
        self.assertEqual(result['id'], task_id)
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(uploader.calls, [('先讲痛点，再讲方案.mp4', 'folder-123')])
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(Path(result['local_file']).read_bytes(), original)

    def test_duplicate_delivery_is_idempotent_but_conflict_rejected(self):
        task_id = self.submit(request_key='message-1')
        self.assertEqual(self.submit(request_key='message-1'), task_id)
        with self.assertRaises(ValueError):
            self.submit(request_key='message-1', comment='另一条点评')
        self.assertEqual(len(self.queue.all()), 1)

    def test_missing_category_never_silently_uploads_to_root(self):
        with self.assertRaises(ValueError):
            self.queue.submit(str(self.source), '点评', '未分类', {})
        self.assertEqual(self.queue.all(), [])

    def test_timeout_never_causes_blind_reupload(self):
        task_id = self.submit()
        uploader = FakeUploader(fail=True)
        self.assertEqual(process_one(self.queue, {}, uploader)['state'], 'needs_review')
        self.queue.recover()
        self.assertIsNone(process_one(self.queue, {}, uploader))
        with self.assertRaises(ValueError):
            self.queue.retry(task_id)
        self.assertEqual(len(uploader.calls), 1)

    def test_real_command_timeout_does_not_upload_early_download_file(self):
        downloader = self.root / 'slow_download.py'
        downloader.write_text(
            'import sys, time\nfrom pathlib import Path\n'
            'Path(sys.argv[1]).write_bytes(b"\\x00\\x00\\x00\\x18ftypmp42" + b"x" * 128)\n'
            'time.sleep(3)\n', encoding='utf-8')
        task_id = self.queue.submit('https://v.douyin.com/offline/', '点评', '营销', {'营销': 'folder-123'})
        uploader = FakeUploader()
        config = {'download_commands': {'douyin': [sys.executable, str(downloader), '{output}']},
                  'download_timeout': 1}
        result = process_one(self.queue, config, uploader)
        self.assertEqual(result['state'], 'prepare_failed')
        self.assertEqual(uploader.calls, [])
        folder = self.queue.root / 'files' / task_id
        self.assertTrue((folder / 'download.mp4').exists(), 'fixture did not write before timeout')
        self.assertFalse((folder / 'prepared.json').exists())

    def test_real_upload_command_timeout_keeps_unknown_result_protection(self):
        marker = self.root / 'upload_fixture_started'
        cli = self.root / 'slow_upload.py'
        cli.write_text(
            'import time, sys, json\nfrom pathlib import Path\n'
            'if "browse" in sys.argv:\n'
            ' folder=Path(sys.argv[sys.argv.index("--task-dir")+1]); listing=folder/"items.jsonl"; listing.write_text("")\n'
            ' print(json.dumps({"type":"result","action":"browse","code":0,"data":{"parent_fid":"folder-123","hasMore":False,"count":0,"file_path":str(listing)}}));sys.exit(0)\n'
            f'Path({str(marker)!r}).write_text("started")\n'
            'print(\'{"type":"result","code":0,"data":{"fids":["fake-only"],"successCount":1}}\', flush=True)\n'
            'time.sleep(3)\n', encoding='utf-8')
        task_id = self.submit()
        config = {'node': sys.executable, 'quark_cli': str(cli), 'quark_authorized': True, 'upload_timeout': 1}
        result = process_one(self.queue, config)
        self.assertTrue(marker.exists(), 'fixture did not start before timeout')
        self.assertEqual(result['state'], 'needs_review')
        self.assertIsNone(result['remote_fid'])
        with self.assertRaises(ValueError):
            self.queue.retry(task_id)

    def test_crash_recovery_distinguishes_local_and_remote_phase(self):
        first = self.submit()
        second = self.submit()
        self.queue.update(first, 'preparing')
        self.queue.update(second, 'uploading')
        self.queue.recover()
        self.assertEqual(self.queue.get(first)['state'], 'queued')
        self.assertEqual(self.queue.get(second)['state'], 'needs_review')

    def test_recovery_reuses_renamed_download_without_fetching_again(self):
        task_id = self.queue.submit('https://v.douyin.com/offline/', '点评', '营销', {'营销': 'folder-123'})
        config = {'download_commands': {'douyin': ['offline-downloader', '{output}']}}
        payload = self.source.read_bytes()

        def offline_download(args, **kwargs):
            output = Path(args[1])
            output.write_bytes(payload)
            output.with_suffix('.receipt.json').write_text('{"fixture": true}')

        with patch('collector.run_managed', side_effect=offline_download) as download:
            local = prepare(self.queue.claim(), self.queue.root, config)
            self.assertEqual(local.name, '点评.mp4')
            self.assertFalse((local.parent / 'download.mp4').exists())
            # Simulate a stop after preparation, before any upload call.
            self.queue.recover()
            uploader = FakeUploader()
            result = process_one(self.queue, config, uploader)
            self.assertEqual(download.call_count, 1)
        self.assertEqual(result['id'], task_id)
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(uploader.calls, [('点评.mp4', 'folder-123')])
        self.assertEqual(Path(result['local_file']).read_bytes(), payload)

    def test_prepared_snapshot_survives_original_local_source_removal(self):
        task_id = self.submit()
        local = prepare(self.queue.claim(), self.queue.root, {})
        payload = local.read_bytes()
        self.source.unlink()
        self.queue.recover()
        result = process_one(self.queue, {}, FakeUploader())
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['id'], task_id)
        self.assertEqual(local.read_bytes(), payload)

    def test_same_size_corruption_is_not_reused(self):
        task_id = self.submit()
        task = self.queue.get(task_id)
        local = prepare(task, self.queue.root, {})
        payload = local.read_bytes()
        local.write_bytes(payload[:-1] + b'X')
        prepare(task, self.queue.root, {})
        self.assertEqual(local.read_bytes(), payload)

    def test_changed_source_or_comment_does_not_reuse_old_snapshot(self):
        task_id = self.submit()
        task = self.queue.get(task_id)
        prepare(task, self.queue.root, {})
        second = self.root / 'other.mp4'
        payload = self.source.read_bytes() + b'other video'
        second.write_bytes(payload)
        task['source'] = str(second)
        local = prepare(task, self.queue.root, {})
        self.assertEqual(local.read_bytes(), payload)
        task['comment'] = '新点评'
        local = prepare(task, self.queue.root, {})
        self.assertEqual(local.name, '新点评.mp4')
        self.assertEqual(local.read_bytes(), payload)

    def test_orphan_or_invalid_receipt_never_authorizes_reuse(self):
        task_id = self.submit()
        task = self.queue.get(task_id)
        local = prepare(task, self.queue.root, {})
        receipt_path = local.parent / 'prepared.json'
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        self.source.unlink()
        foreign = dict(receipt, task=dict(receipt['task'], id='another-task'))
        escaped = dict(receipt, filename='../' + local.name)
        cases = [None, 'invalid json', '[]', json.dumps(foreign), json.dumps(escaped)]
        for value in cases:
            with self.subTest(receipt=value):
                if value is None:
                    receipt_path.unlink(missing_ok=True)
                else:
                    receipt_path.write_text(value, encoding='utf-8')
                with self.assertRaises(ValueError):
                    prepare(task, self.queue.root, {})

    def test_dtk_output_also_passes_common_validation(self):
        for index, payload in enumerate((b'', b'<html>error</html>', b'{"error":"unavailable"}')):
            with self.subTest(payload=payload):
                task_id = self.queue.submit('https://v.douyin.com/offline/', '点评', '营销',
                                            {'营销': 'folder-123'}, request_key=f'dtk-{index}')
                def offline_download(config, source, output, **kwargs):
                    Path(output).write_bytes(payload)
                uploader = FakeUploader()
                with patch('dtk_download.download', side_effect=offline_download):
                    result = process_one(self.queue, {'dtk': {'base_url': 'http://offline.invalid'}}, uploader)
                self.assertEqual(result['id'], task_id)
                self.assertEqual(result['state'], 'prepare_failed')
                self.assertEqual(uploader.calls, [])
                self.assertFalse((self.queue.root / 'files' / task_id / 'prepared.json').exists())

    def test_html_error_document_is_not_uploaded(self):
        self.source.write_text('<html>login required</html>')
        self.submit()
        uploader = FakeUploader()
        self.assertEqual(process_one(self.queue, {}, uploader)['state'], 'prepare_failed')
        self.assertEqual(uploader.calls, [])

    def test_unconfigured_share_downloader_is_visible_failure(self):
        self.queue.submit('https://v.douyin.com/example/', '点评', '营销', {'营销': 'folder-123'})
        uploader = FakeUploader()
        self.assertEqual(process_one(self.queue, {}, uploader)['state'], 'prepare_failed')
        self.assertEqual(uploader.calls, [])

    def test_unconfigured_quark_does_not_try_downloading(self):
        self.submit()
        result = process_one(self.queue, {})
        self.assertEqual(result['state'], 'setup_required')
        self.assertFalse((self.queue.root / 'files').exists())

    def test_os_worker_lock_prevents_second_worker(self):
        with WorkerLock(self.queue.root):
            with self.assertRaises(RuntimeError):
                with WorkerLock(self.queue.root):
                    self.fail('second worker acquired exclusive lock')
        with WorkerLock(self.queue.root):
            pass

    def test_success_requires_remote_identity_and_final_result(self):
        line = {'type': 'result', 'code': 0, 'data': {'fids': ['remote'], 'successCount': 1}}
        self.assertEqual(parse_upload(json.dumps(line), 0)['state'], 'succeeded')
        self.assertEqual(parse_upload(json.dumps(line), 1)['state'], 'needs_review')
        self.assertEqual(parse_upload('{"type":"progress","data":{"percent":100}}', 0)['state'], 'needs_review')
        self.assertEqual(parse_upload('{"type":"result","code":0}', 0)['state'], 'needs_review')

    def test_service_notice_is_preserved_on_failure(self):
        line = {'type': 'result', 'code': -204, 'msg': '上传失败', 'notice': {'text': '原样提示'}}
        self.assertEqual(parse_upload(json.dumps(line), 1)['notice'], '原样提示')

    def test_safe_names_preserve_chinese_and_extension(self):
        self.assertEqual(video_name(' 好的开头：问题/冲突? ', '.MP4'), '好的开头：问题 冲突.mp4')
        self.assertEqual(video_name('CON', '.mp4'), '_CON.mp4')
        self.assertLessEqual(len(video_name('😀' * 200, '.mp4').encode('utf-16-le')), 248)
        with self.assertRaises(ValueError):
            video_name('///', '.mp4')

    def test_android_douyin_share_text(self):
        text = '1.07 :7pm 10/01 E@u.Sl DUY:/ 《太平洋战争》深度解说第3集 #影视解说 https://v.douyin.com/6AymbiXXYOg/ 复制此链接，打开Dou音搜索，直接观看视频！'
        self.assertEqual(parse_share(text), {'url': 'https://v.douyin.com/6AymbiXXYOg/', 'platform': 'douyin'})

    def test_wechat_markdown_share(self):
        self.assertEqual(parse_share('[https://weixin.qq.com/sph/AwRoUnmCm0](https://weixin.qq.com/sph/AwRoUnmCm0)'),
                         {'url': 'https://weixin.qq.com/sph/AwRoUnmCm0', 'platform': 'wechat'})

    def test_multiple_videos_are_not_silently_paired_to_one_comment(self):
        with self.assertRaises(ValueError):
            parse_share('https://v.douyin.com/one/ https://weixin.qq.com/sph/two')

    def test_unrecognized_share_host_rejected(self):
        with self.assertRaises(ValueError):
            parse_share('https://v.douyin.com.attacker.example/video/123')


    def test_user_category_routes_upload_without_tag_in_filename(self):
        task_id = self.queue.submit_note(str(self.source), '这个开头值得借鉴 #营销技巧', {'营销技巧': 'marketing-folder'})
        uploader = FakeUploader()
        result = process_one(self.queue, {}, uploader)
        self.assertEqual(result['id'], task_id)
        self.assertEqual(uploader.calls, [('这个开头值得借鉴.mp4', 'marketing-folder')])
        self.assertEqual(result['input_text'], '这个开头值得借鉴 #营销技巧')

    def test_invalid_notes_never_enter_upload_queue(self):
        categories = {'营销技巧': 'marketing-folder', '农业': 'farm-folder'}
        for note in ('只有点评', '#营销技巧', '点评 #不存在', '点评 #营销技巧 #农业'):
            with self.subTest(note=note), self.assertRaises(ValueError):
                self.queue.submit_note(str(self.source), note, categories)
        self.assertEqual(self.queue.all(), [])

    def test_share_hashtags_do_not_override_review_category(self):
        source = '影视解说 #二战 #美剧 https://v.douyin.com/6AymbiXXYOg/'
        task_id = self.queue.submit_note(source, '这个开头值得借鉴 #营销技巧', {'营销技巧': 'marketing-folder'})
        self.assertEqual(self.queue.get(task_id)['category'], '营销技巧')
        self.assertEqual(self.queue.get(task_id)['comment'], '这个开头值得借鉴')

    def test_fullwidth_tag_and_multiline_note(self):
        self.assertEqual(parse_note('先讲痛点\n再给方案\n＃营销技巧', {'营销技巧': 'fid'}),
                         ('先讲痛点\n再给方案', '营销技巧'))


if __name__ == '__main__':
    unittest.main()
