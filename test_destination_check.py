"""Offline manifests, real local CLI fixture, and concurrent queue transactions."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from collector import Queue, prepare, process_one, sha256
from destination_check import AgentEnvironmentUnsupported, DestinationUnchecked, NameConflict, inspect_result
from test_collector import FakeUploader


class DestinationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / 'check'; self.directory.mkdir()
        self.queue = Queue(self.root / 'data')
        self.source = self.root / 'source.mp4'; self.source.write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'x' * 128)
        self.id = self.submit()
        self.task = self.queue.get(self.id)

    def tearDown(self):
        self.queue.db.close(); self.temp.cleanup()

    def submit(self, comment='点评', folder='folder'):
        return self.queue.submit(str(self.source), comment, '二创', {'二创': folder})

    def result(self, entries, **changes):
        artifact = self.directory / 'items.jsonl'
        artifact.write_text(''.join(json.dumps(e)+'\n' for e in entries), encoding='utf-8')
        data = dict(parent_fid='folder', count=len(entries), hasMore=False, file_path=str(artifact))
        data.update(changes)
        return json.dumps(dict(action='browse', type='result', code=0, data=data, notice={'text':'原样提示'}))

    def test_conflict_beyond_five_item_preview_and_unicode_case(self):
        for name, target in [('点评.mp4','点评.mp4'), ('É.MP4','e\u0301.mp4')]:
            entries = [dict(fid=str(i),filename=f'其他{i}') for i in range(7)] + [dict(fid='last',filename=name)]
            with self.assertRaises(NameConflict) as caught:
                inspect_result(self.result(entries), 0, self.directory, self.task, target)
            self.assertEqual(caught.exception.notice, '原样提示')
            self.assertTrue(json.loads((self.directory/'check.json').read_text(encoding='utf-8'))['conflict'])

    def test_empty_or_other_names_are_complete_checks(self):
        for entries in ([], [dict(fid='f',filename='其他.mp4')]):
            self.assertEqual(inspect_result(self.result(entries),0,self.directory,self.task,'点评.mp4'),'原样提示')

    def test_agent_environment_refusal_is_not_reported_as_folder_problem(self):
        output = json.dumps({'type': 'result', 'action': 'runtime', 'code': -104,
                             'msg': '无法识别当前 Agent 环境，禁止继续使用', 'notice': {'text': '原样提示'}})
        with self.assertRaises(AgentEnvironmentUnsupported) as caught:
            inspect_result(output, 1, self.directory, self.task, '点评.mp4')
        self.assertIn('-104', str(caught.exception))
        self.assertEqual(caught.exception.notice, '原样提示')
        uploader = FakeUploader()
        with patch.object(uploader, 'check_destination', side_effect=caught.exception):
            result = process_one(self.queue, {}, uploader)
        self.assertEqual(result['state'], 'setup_required')
        self.assertEqual(uploader.calls, [])

    def test_incomplete_wrong_or_malformed_manifests_never_allow_upload(self):
        for changes in ({'hasMore':True}, {'hasMore':None}, {'count':1}, {'count':False},
                        {'parent_fid':'elsewhere'}, {'file_path':str(self.source)}):
            with self.subTest(changes=changes), self.assertRaises(DestinationUnchecked):
                inspect_result(self.result([],**changes),0,self.directory,self.task,'点评.mp4')
        for entry in ({'fid':'x'}, {'fid':'x','filename':'x','parent_fid':'other'}, []):
            with self.assertRaises(DestinationUnchecked):
                inspect_result(self.result([entry]),0,self.directory,self.task,'点评.mp4')
        for output, code in [('garbage',0), (self.result([]),1), (self.result([])+'\n'+self.result([]),0)]:
            with self.assertRaises(DestinationUnchecked):
                inspect_result(output,code,self.directory,self.task,'点评.mp4')

    def test_failed_check_keeps_cache_and_retry_checks_again(self):
        uploader = FakeUploader()
        with patch.object(uploader,'check_destination',side_effect=DestinationUnchecked(notice='保留提示')):
            result = process_one(self.queue,{},uploader)
        self.assertEqual(result['state'],'destination_unchecked'); self.assertEqual(result['notice'],'保留提示')
        self.assertEqual(uploader.calls,[])
        self.queue.retry(self.id)
        with patch('collector.shutil.copyfile',side_effect=AssertionError('Do not copy twice')):
            self.assertEqual(process_one(self.queue,{},uploader)['state'],'succeeded')

    def test_conflict_is_not_a_retryable_upload_and_can_be_cancelled(self):
        uploader = FakeUploader()
        with patch.object(uploader,'check_destination',side_effect=NameConflict('提示')):
            result = process_one(self.queue,{},uploader)
        self.assertEqual(result['state'],'name_conflict'); self.assertEqual(uploader.calls,[])
        with self.assertRaises(ValueError): self.queue.retry(self.id)
        self.queue.recover(); self.assertEqual(self.queue.get(self.id)['state'],'name_conflict')
        self.assertEqual(self.queue.cancel(self.id)['state'],'cancelled')

    def test_cancel_during_directory_check_wins_over_late_failure(self):
        uploader = FakeUploader()
        def checking(*args,**kwargs):
            self.queue.cancel(self.id); raise DestinationUnchecked()
        with patch.object(uploader,'check_destination',side_effect=checking):
            self.assertEqual(process_one(self.queue,{},uploader)['state'],'cancelled')
        self.assertEqual(uploader.calls,[])

    def test_retry_does_not_reuse_old_service_notices(self):
        uploader=FakeUploader()
        for notice in ('上次提示',''):
            with patch.object(uploader,'check_destination',side_effect=DestinationUnchecked(notice=notice)):
                result=process_one(self.queue,{},uploader)
            self.assertEqual(result['notice'],notice)
            self.queue.retry(self.id)

    def test_tracked_success_unknown_and_inflight_block_same_name_only_in_same_folder(self):
        uploader = FakeUploader(); process_one(self.queue,{},uploader)
        for state in ('succeeded','needs_review','uploading'):
            self.queue.update(self.id,state)
            next_id = self.submit()
            self.assertEqual(process_one(self.queue,{},uploader)['state'],'name_conflict')
            self.assertIsNone(self.queue.get(next_id)['upload_attempt'])
        self.submit(folder='different-folder')
        self.assertEqual(process_one(self.queue,{},uploader)['state'],'succeeded')
        self.assertEqual(len(uploader.calls),2)

    def test_two_simultaneous_upload_reservations_only_one_wins(self):
        second = self.submit(); tasks=[self.queue.claim(),self.queue.claim()]
        prepared=[prepare(task,self.queue.root,{}) for task in tasks]
        barrier=threading.Barrier(2)
        def reserve(task,path):
            queue=Queue(self.queue.root)
            try:
                barrier.wait(timeout=3)
                try: queue.start_upload(task,path,sha256(path)); return 'uploading'
                except NameConflict: return 'conflict'
            finally: queue.db.close()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures=[pool.submit(reserve,t,p) for t,p in zip(tasks,prepared)]
            self.assertCountEqual([f.result(timeout=5) for f in futures],['uploading','conflict'])


if __name__=='__main__': unittest.main()
