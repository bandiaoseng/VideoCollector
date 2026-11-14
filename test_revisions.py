"""Revision identity, transaction races, and verified local media reuse."""
import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from collector import process_one, revision_media, sha256
from destination_check import NameConflict
from test_collector import FakeUploader
from web_store import DraftStore


class RevisionTest(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'socket.socket.connect_ex'):
            guard = patch(target, side_effect=AssertionError('Network forbidden'))
            guard.start(); self.addCleanup(guard.stop)
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.store=DraftStore(self.root/'data'); self.queue=self.store.queue
        self.categories={'二创':'folder-a','其他':'folder-b'}
        self.draft=self.store.create(str(uuid.uuid4()),'https://v.douyin.com/offline/')
        self.store.save_note(self.draft['id'],'旧点评 #二创',0)
        self.task=self.store.submit(self.draft['id'],'旧点评 #二创',self.categories,1)
        self.media=b'\x00\x00\x00\x18ftypmp42'+b'x'*128
        self.config={'download_commands':{'douyin':[sys.executable,'-c',
            'import sys;from pathlib import Path;Path(sys.argv[1]).write_bytes('+repr(self.media)+')','{output}']}}
        uploader=FakeUploader()
        with patch.object(uploader,'check_destination',side_effect=NameConflict()):
            self.assertEqual(process_one(self.queue,self.config,uploader)['state'],'name_conflict')
        self.original=self.queue.get(self.task['task_id'])
        self.original_media=self.queue.root/'files'/self.original['id']/'旧点评.mp4'

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def revised(self):
        return self.store.revise(self.draft['id'])['draft']

    def submit_revision(self,child,note='新点评 #其他'):
        saved=self.store.save_note(child['id'],note,child['version'])
        return self.store.submit(child['id'],note,self.categories,saved['version'])

    def test_repeated_clicks_return_one_draft_and_preserve_history(self):
        child=self.revised()
        self.assertEqual(self.revised(),child)
        self.assertEqual(child['source'],self.draft['source'])
        self.assertEqual(child['note'],'旧点评 #二创');self.assertIsNone(child['task_id'])
        self.assertEqual(len(self.queue.all()),1)
        self.assertEqual(self.store.get(self.draft['id'])['state'],'superseded')
        self.assertEqual(self.store.get(self.draft['id'])['note'],'旧点评 #二创')
        self.queue.recover();self.assertIsNone(self.queue.claim())
        with self.assertRaises(ValueError):self.queue.retry(self.original['id'])
        with self.assertRaises(ValueError):self.store.cancel(self.draft['id'])

    def test_new_note_and_category_reuse_verified_video_without_redownloading(self):
        child=self.revised();task=self.submit_revision(child)
        uploader=FakeUploader()
        with patch('collector.run_managed',side_effect=AssertionError('Redownload is not needed')):
            result=process_one(self.queue,self.config,uploader)
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(uploader.calls,[('新点评.mp4','folder-b')])
        self.assertEqual(Path(result['local_file']).read_bytes(),self.media)
        self.assertEqual(self.original_media.read_bytes(),self.media)
        self.assertEqual(result['prepared_from'],self.original['id'])
        self.assertEqual(result['file_hash'],sha256(self.original_media))
        self.assertEqual(self.store.revise(self.draft['id'])['draft']['task_id'],task['task_id'])

    def test_corrupt_cache_is_not_reused(self):
        child=self.revised();self.submit_revision(child)
        self.original_media.write_bytes(b'corrupt')
        uploader=FakeUploader()
        with patch('collector.run_managed',side_effect=AssertionError('Redownload forbidden')) as downloader:
            result=process_one(self.queue,self.config,uploader)
        downloader.assert_not_called()
        self.assertEqual(result['state'],'prepare_failed');self.assertEqual(uploader.calls,[])
        self.assertIn('不会重新解析或下载', result['error'])

    def test_changed_source_cannot_reuse_another_video(self):
        child=self.revised();new=self.submit_revision(child)
        self.store.db.execute('UPDATE tasks SET source=? WHERE id=?',('https://v.douyin.com/different/',new['task_id']))
        self.store.db.commit()
        with patch('collector.run_managed',side_effect=AssertionError('Redownload forbidden')) as downloader:
            result=process_one(self.queue,self.config,FakeUploader())
        downloader.assert_not_called();self.assertEqual(result['state'],'prepare_failed')

    def test_missing_revision_cache_stays_local_only_after_retry_and_restart(self):
        child=self.revised();new=self.submit_revision(child)
        self.original_media.unlink()
        uploader=FakeUploader()
        with patch('collector.run_managed',side_effect=AssertionError('Redownload forbidden')) as downloader:
            for restart in (False, False, True):
                if restart:
                    self.queue.retry(new['task_id'])
                    self.assertEqual(self.queue.claim()['state'], 'preparing')
                    self.store.close()
                    self.store=DraftStore(self.root/'data');self.queue=self.store.queue
                    self.queue.recover()
                elif self.queue.get(new['task_id'])['state']=='prepare_failed':
                    self.queue.retry(new['task_id'])
                result=process_one(self.queue,self.config,uploader)
                self.assertEqual(result['state'],'prepare_failed')
                self.assertIn('不会重新解析或下载',result['error'])
            downloader.assert_not_called()
        self.assertEqual(uploader.calls,[])

    def test_missing_receipt_invalid_reference_and_original_state_never_download(self):
        child=self.revised();new=self.submit_revision(child)
        receipt=self.original_media.parent/'prepared.json'
        saved_receipt=receipt.read_bytes()
        uploader=FakeUploader()
        with patch('collector.run_managed',side_effect=AssertionError('Redownload forbidden')) as downloader:
            for reason in ('missing-receipt','malformed-reference','missing-original','unknown-upload'):
                with self.subTest(reason=reason):
                    self.queue.db.execute('UPDATE tasks SET prepared_from=? WHERE id=?',
                        (self.original['id'],new['task_id']))
                    self.queue.update(self.original['id'],'superseded')
                    receipt.write_bytes(saved_receipt)
                    if reason=='missing-receipt':receipt.unlink()
                    elif reason=='malformed-reference':
                        self.queue.db.execute('UPDATE tasks SET prepared_from=? WHERE id=?',('../outside',new['task_id']))
                    elif reason=='missing-original':
                        self.queue.db.execute('UPDATE tasks SET prepared_from=? WHERE id=?',('f'*32,new['task_id']))
                    else:self.queue.update(self.original['id'],'needs_review')
                    self.queue.db.commit()
                    if self.queue.get(new['task_id'])['state']=='prepare_failed':self.queue.retry(new['task_id'])
                    result=process_one(self.queue,self.config,uploader)
                    self.assertEqual(result['state'],'prepare_failed')
                    self.assertIn('不会重新解析或下载',result['error'])
            downloader.assert_not_called()
        self.assertEqual(uploader.calls,[])

    def test_revision_disappearing_after_validation_does_not_fall_back_to_download(self):
        child=self.revised();self.submit_revision(child)
        def vanished(task,root):
            source=revision_media(task,root)
            source.unlink()
            return source
        uploader=FakeUploader()
        with patch('collector.revision_media',side_effect=vanished), \
                patch('collector.run_managed',side_effect=AssertionError('Redownload forbidden')) as downloader:
            result=process_one(self.queue,self.config,uploader)
        downloader.assert_not_called()
        self.assertEqual(result['state'],'prepare_failed');self.assertEqual(uploader.calls,[])

    def test_wechat_revision_cache_failure_cannot_reach_parser_or_dtk(self):
        child=self.revised();new=self.submit_revision(child)
        source='https://weixin.qq.com/sph/offline-test/'
        self.queue.db.execute('UPDATE tasks SET source=? WHERE id IN (?,?)',
            (source,self.original['id'],new['task_id']))
        self.queue.db.commit()
        config={'zhiling':{'enabled':True},'dtk':{'base_url':'https://offline.invalid'}}
        uploader=FakeUploader()
        with patch('zhiling_download.check_settings',side_effect=AssertionError('Parser setup forbidden')) as parser, \
                patch('collector.run_managed',side_effect=AssertionError('Redownload forbidden')) as downloader, \
                patch('dtk_download.download',side_effect=AssertionError('DTK forbidden')) as dtk:
            result=process_one(self.queue,config,uploader)
            parser.assert_not_called();downloader.assert_not_called();dtk.assert_not_called()
        self.assertEqual(result['state'],'prepare_failed');self.assertEqual(uploader.calls,[])

    def test_unsafe_states_cannot_be_revised(self):
        for state in ('uploading','needs_review','succeeded','preparing','queued','cancelled'):
            self.queue.update(self.original['id'],state)
            with self.subTest(state=state),self.assertRaises(ValueError):self.revised()
        self.assertEqual(len(self.store.all()),1)

    def test_cancelled_revision_is_not_recreated_by_retrying_original_request(self):
        child=self.revised();self.store.cancel(child['id'])
        repeated=self.revised()
        self.assertEqual(repeated['id'],child['id']);self.assertEqual(repeated['state'],'cancelled')
        with self.assertRaises(ValueError):self.submit_revision(repeated)

    def test_concurrent_revision_requests_share_one_child(self):
        barrier=threading.Barrier(2)
        def revise():
            store=DraftStore(self.queue.root)
            try:
                barrier.wait(timeout=3);return store.revise(self.draft['id'])['draft']['id']
            finally:store.close()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures=[pool.submit(revise) for _ in range(2)]
            ids=[f.result(timeout=5) for f in futures]
        self.assertEqual(ids[0],ids[1]);self.assertEqual(len(self.store.all()),2)

    def test_cancel_and_revise_have_one_winner(self):
        barrier=threading.Barrier(2)
        def operation(revise):
            store=DraftStore(self.queue.root)
            try:
                barrier.wait(timeout=3)
                try:
                    if revise:store.revise(self.draft['id'])
                    else:store.cancel(self.draft['id'])
                    return True
                except ValueError:return False
            finally:store.close()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures=[pool.submit(operation,mode) for mode in (True,False)]
            outcomes=[f.result(timeout=5) for f in futures]
        self.assertEqual(sum(outcomes),1)
        self.assertEqual(self.store.get(self.draft['id'])['state'],'superseded' if outcomes[0] else 'cancelled')


if __name__=='__main__':unittest.main()
