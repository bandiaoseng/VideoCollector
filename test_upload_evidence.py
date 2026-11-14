"""Offline recovery tests: the CLI fixture is local Python, never Quark."""
import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from collector import Queue, process_one
from upload_evidence import evidence_directory


def receipt_rows(task):
    expected = json.loads(task['upload_snapshot'])
    return [
        {'action': 'upload', 'type': 'list', 'code': 0,
         'data': {'recordId': 'offline-record', 'fileId': 'offline-fid',
                  'fileName': expected['filename'], 'fileSize': expected['bytes']}},
        {'action': 'upload', 'type': 'result', 'code': 0,
         'data': {'fileNames': [expected['filename']], 'fileCount': 1, 'totalSize': expected['bytes'],
                  'fids': ['offline-fid'], 'successCount': 1}, 'notice': {'text': '测试回执提示'}}
    ]


def write_receipt(root, task, rows=None):
    path = evidence_directory(root, task) / 'stdout.ndjson'
    rows = receipt_rows(task) if rows is None else rows
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
    return path


class UploadEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='collector receipt ')
        self.root = Path(self.temp.name)
        self.queue = Queue(self.root / 'data')
        self.source = self.root / 'source.mp4'
        self.source.write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'x' * 128)
        self.task_id = self.queue.submit(str(self.source), '测试点评', '二创', {'二创': 'offline-folder'})
        self.cli = self.root / 'fake_cli.py'

    def tearDown(self):
        self.queue.db.close()
        self.temp.cleanup()

    def upload_fixture(self, mode='normal'):
        # The fixture constructs its receipt from the actual invocation arguments.
        self.cli.write_text('''import json, sys, time
from pathlib import Path
if 'browse' in sys.argv:
 folder = Path(sys.argv[sys.argv.index('--task-dir') + 1])
 listing = folder / 'items.jsonl'; listing.write_text('')
 print(json.dumps({'action':'browse','type':'result','code':0,'data':{'parent_fid':'offline-folder','file_path':str(listing),'count':0,'hasMore':False}}))
 sys.exit(0)
path = Path(sys.argv[sys.argv.index('--file-path') + 1])
folder = sys.argv[sys.argv.index('--parent-fid') + 1]
assert folder == 'offline-folder'
rows = [
 {'action':'upload', 'type':'list', 'code':0, 'data':{'recordId':'offline-record', 'fileId':'offline-fid', 'fileName':path.name, 'fileSize':path.stat().st_size}},
 {'action':'upload', 'type':'result', 'code':0, 'data':{'fileNames':[path.name], 'fileCount':1, 'totalSize':path.stat().st_size, 'fids':['offline-fid'], 'successCount':1}, 'notice':{'text':'测试回执提示'}}
]
''' + f'mode = {mode!r}\n' + '''
for row in (rows[:1] if mode == 'partial' else rows):
 print(json.dumps(row), flush=True)
if mode == 'partial':
 print('{"type":"result",', end='', flush=True)
if mode != 'normal': time.sleep(3)
''', encoding='utf-8')
        config = {'node': sys.executable, 'quark_cli': str(self.cli),
                  'quark_authorized': True, 'upload_timeout': 1}
        return process_one(self.queue, config)

    def lost_commit(self):
        task = self.upload_fixture()
        self.assertEqual(task['state'], 'succeeded')
        self.queue.update(self.task_id, 'uploading', remote_fid=None)
        return self.queue.get(self.task_id)

    def test_restart_recovers_success_receipt_without_launching_another_command(self):
        self.lost_commit()
        self.queue.db.close()
        self.queue = Queue(self.root / 'data')
        with patch('collector.run_managed') as launch:
            self.queue.recover()
            task = self.queue.get(self.task_id)
            self.assertEqual(task['state'], 'succeeded')
            self.assertEqual(task['remote_fid'], 'offline-fid')
            self.assertEqual(task['notice'], '测试回执提示')
            self.assertIn('没有重复上传', task['review_note'])
            self.queue.review_upload(self.task_id)
            launch.assert_not_called()
        audit = self.queue.db.execute('SELECT * FROM upload_reviews').fetchall()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]['outcome'], 'confirmed')
        self.assertEqual(len(json.loads(audit[0]['evidence'])['receipt_sha256']), 64)

    def test_timeout_after_complete_receipt_is_reconciled_automatically(self):
        task = self.upload_fixture('hang')
        self.assertEqual(task['state'], 'succeeded')
        self.assertTrue((evidence_directory(self.queue.root, task) / 'stdout.ndjson').is_file())
        self.assertIn('完整上传回执', task['review_note'])

    def test_partial_receipt_stays_unknown_and_cannot_reupload(self):
        task = self.upload_fixture('partial')
        self.assertEqual(task['state'], 'needs_review')
        self.assertIn('仍需核对网盘', task['review_note'])
        with self.assertRaises(ValueError):
            self.queue.retry(self.task_id)
        self.queue.recover()
        self.assertEqual(self.queue.get(self.task_id)['state'], 'needs_review')

    def test_incomplete_conflicting_and_wrong_file_receipts_do_not_authorize_success(self):
        task = self.lost_commit()
        original = receipt_rows(task)
        mutations = [
            lambda rows: rows[1]['data'].update(totalSize=1),
            lambda rows: rows[1]['data'].update(fileNames=['another.mp4']),
            lambda rows: rows[0]['data'].update(fileSize=1),
            lambda rows: rows[0]['data'].update(fileName='another.mp4'),
            lambda rows: rows[0]['data'].update(fileId='another-fid'),
            lambda rows: rows[1]['data'].update(fids='offline-fid'),
            lambda rows: rows[1]['data'].update(successCount=True),
            lambda rows: rows[1]['data'].update(failedCount=1),
            lambda rows: rows[1].update(code=-204),
            lambda rows: rows[0].update(code=-204),
            lambda rows: rows[1].update(action='search'),
            lambda rows: rows[1].update(notice=['invalid']),
            lambda rows: rows.pop(),
            lambda rows: rows.pop(0),
            lambda rows: rows.append(rows[1]),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                rows = json.loads(json.dumps(original))
                mutate(rows)
                write_receipt(self.queue.root, task, rows)
                self.queue.update(self.task_id, 'needs_review')
                result = self.queue.review_upload(self.task_id)
                self.assertEqual(result['state'], 'needs_review')
                self.assertIsNone(result['remote_fid'])

    def test_receipt_is_bound_to_task_folder_comment_source_and_hash(self):
        task = self.lost_commit()
        original = json.loads(task['upload_snapshot'])
        for key in ('task_id', 'parent_fid', 'comment', 'source', 'sha256', 'filename'):
            with self.subTest(key=key):
                changed = {**original, key: 'unrelated'}
                self.queue.db.execute('UPDATE tasks SET upload_snapshot=?,state=? WHERE id=?',
                                      (json.dumps(changed), 'needs_review', self.task_id))
                self.queue.db.commit()
                self.assertEqual(self.queue.review_upload(self.task_id)['state'], 'needs_review')

    def test_old_tasks_without_evidence_stay_unknown_and_active_uploads_are_not_reviewed(self):
        for state in ('queued', 'preparing', 'uploading'):
            self.queue.update(self.task_id, state)
            with self.assertRaises(ValueError):
                self.queue.review_upload(self.task_id)
            self.assertEqual(self.queue.get(self.task_id)['state'], state)
        self.queue.update(self.task_id, 'needs_review')
        self.assertEqual(self.queue.review_upload(self.task_id)['state'], 'needs_review')
        with self.assertRaises(ValueError):
            self.queue.retry(self.task_id)

    def test_concurrent_reviews_record_one_success(self):
        self.lost_commit()
        self.queue.update(self.task_id, 'needs_review')
        def check(_):
            queue = Queue(self.queue.root)
            try:
                return queue.review_upload(self.task_id)['state']
            finally:
                queue.db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(check, range(4))), ['succeeded'] * 4)
        self.assertEqual(self.queue.db.execute('SELECT COUNT(*) FROM upload_reviews').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
