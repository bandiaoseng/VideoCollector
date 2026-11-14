"""Capacity failures use tiny fixtures and synthetic free-space readings only."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from collector import Queue, prepare, process_one
from parse_recovery import parser_recovery
from process_runner import run_managed
from service_check import readiness
from storage_guard import StorageError, budget, check_free, policy, status
from test_collector import FakeUploader
from test_dtk_download import FakeClient as DtkClient
from test_process_runner import is_running
from zhiling_download import download as wechat_download, ZhilingError


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = Queue(self.root/'data'); self.addCleanup(self.queue.db.close)
        self.config = {'storage': {'reserve_bytes': 10, 'download_budget_bytes': 1000}}
        self.free = 10000000
        guard = patch('storage_guard.shutil.disk_usage', side_effect=lambda _: SimpleNamespace(free=self.free))
        guard.start(); self.addCleanup(guard.stop)
        for target in ('socket.socket.connect', 'socket.socket.connect_ex'):
            guard = patch(target, side_effect=AssertionError('No real service calls'))
            guard.start(); self.addCleanup(guard.stop)

    def task(self, source='https://weixin.qq.com/sph/offline-storage'):
        ident = self.queue.submit_note(source, '空间测试 #二创', {'二创': 'fixture-folder'})
        return self.queue.get(ident)

    def source(self, size=100):
        source = self.root/'source.mp4'
        source.write_bytes(b'\0\0\0\x18ftypmp42' + b'x'*size)
        return source

    def test_low_reserve_stops_before_uploader_preflight_or_parser(self):
        self.task(); self.free = 9
        uploader = FakeUploader()
        with patch.object(uploader,'preflight',side_effect=AssertionError('No preflight')), patch('collector.run_managed',side_effect=AssertionError('No parser')):
            result = process_one(self.queue,self.config,uploader)
        self.assertEqual(result['state'],'setup_required')
        self.assertIn('空间不足',result['error'])
        self.assertEqual(uploader.calls,[])

    def test_not_enough_download_budget_stops_before_first_parse_and_preserves_marker(self):
        task = self.task(); self.free = 500
        folder=self.queue.root/'files'/task['id'];folder.mkdir(parents=True)
        marker=folder/'download.zhiling-private.json'
        marker.write_text(json.dumps({'state':'request_started','source_hash':hashlib.sha256(task['source'].encode()).hexdigest()}))
        original=marker.read_bytes()
        with patch('collector.run_managed',side_effect=AssertionError('No parser')):
            result=process_one(self.queue,self.config,FakeUploader())
        self.assertEqual(result['state'],'prepare_failed')
        self.assertIn('空间不足',result['error'])
        self.assertEqual(marker.read_bytes(),original)
        self.assertTrue(parser_recovery(folder,task['source'])['parse_retry_required'])

    def test_valid_cache_needs_reserve_only_and_is_never_redownloaded(self):
        task=self.task(str(self.source())); local=prepare(task,self.queue.root,self.config)
        self.free=500
        with patch('collector.run_managed',side_effect=AssertionError('No download')):
            self.assertEqual(prepare(task,self.queue.root,self.config),local)
            uploader=FakeUploader(); result=process_one(self.queue,self.config,uploader)
        self.assertEqual(result['state'],'succeeded');self.assertEqual(len(uploader.calls),1)

    def test_local_copy_checks_actual_size_before_writing(self):
        source=self.source(2000); original=source.read_bytes();task=self.task(str(source));self.free=500
        with self.assertRaises(StorageError):prepare(task,self.queue.root,self.config)
        self.assertEqual(source.read_bytes(),original)
        self.assertFalse((self.queue.root/'files'/task['id']/'copy.partial').exists())

    def test_local_copy_stops_when_another_process_consumes_reserve(self):
        source=self.source(2*1024**2);task=self.task(str(source))
        readings=iter([10000000,10000000,10000000,9])
        with patch('storage_guard.shutil.disk_usage',side_effect=lambda _:SimpleNamespace(free=next(readings))):
            with self.assertRaises(StorageError):prepare(task,self.queue.root,self.config)
        folder=self.queue.root/'files'/task['id']
        self.assertFalse((folder/'prepared.json').exists())
        self.assertTrue(source.is_file())

    def test_oversized_generic_download_is_not_uploaded(self):
        task=self.task('https://v.douyin.com/offline-storage/')
        config={**self.config,'download_command':['synthetic','{source}','{output}']}
        def downloader(args,**kwargs):Path(args[-1]).write_bytes(b'x'*1001)
        uploader=FakeUploader()
        with patch('collector.run_managed',side_effect=downloader):result=process_one(self.queue,config,uploader)
        self.assertEqual(result['state'],'prepare_failed');self.assertIn('预算',result['error']);self.assertEqual(uploader.calls,[])

    def test_dtk_stream_failure_preserves_remote_task_and_resumes_without_new_parse(self):
        from dtk_download import download
        client=DtkClient();output=self.root/'download.mp4'
        def checkpoint():
            if hasattr(client,'asserted_route'):raise StorageError('synthetic low disk')
            return False
        with self.assertRaises(StorageError):download({},'synthetic-source',output,client=client,cancel_check=checkpoint)
        self.assertFalse(output.exists());self.assertTrue(output.with_suffix('.dtk.json').is_file())
        download({},'synthetic-source',output,client=client)
        self.assertEqual(sum(route=='/api/v1/parse' for route,body in client.calls),1)

    def test_running_child_is_stopped_and_unknown_parse_barrier_survives(self):
        task=self.task();folder=self.queue.root/'files'/task['id'];marker=folder/'download.zhiling-private.json'
        script=self.root/'fake_parser.py'
        script.write_text("import json,sys,os,time;from pathlib import Path;p=Path(sys.argv[1]);p.with_name('child.pid').write_text(str(os.getpid()));p.write_text(json.dumps({'state':'request_started','source_hash':sys.argv[2]}));time.sleep(30)")
        config={**self.config,'zhiling':{'enabled':True,'max_bytes':1000}}
        def managed(args,**kwargs):
            return run_managed([sys.executable,str(script),str(marker),hashlib.sha256(task['source'].encode()).hexdigest()],timeout=5,cancel_check=kwargs['cancel_check'])
        def space(_):return SimpleNamespace(free=9 if marker.exists() else 10000000)
        with patch('storage_guard.shutil.disk_usage',side_effect=space),patch('collector.run_managed',side_effect=managed),patch('zhiling_download.check_settings'):
            result=process_one(self.queue,config,FakeUploader())
        self.assertEqual(result['state'],'prepare_failed');self.assertIn('空间不足',result['error'])
        self.assertFalse(is_running(int((folder/'child.pid').read_text())))
        self.assertTrue(parser_recovery(folder,task['source'])['parse_retry_required'])
        class NoParser:
            def parse(self,*a):raise AssertionError('Must not reparse after disk recovery')
        with patch('zhiling_download.check_settings'),self.assertRaises(ZhilingError):
            wechat_download({},task['source'],folder/'download.mp4',client=NoParser())

    def test_invalid_policy_and_disk_query_failure_fail_closed_without_leaking_path(self):
        for value in (None,[],False,{'reserve_bytes':True},{'download_budget_bytes':0}):
            with self.assertRaises(StorageError):policy({'storage':value})
        with patch('storage_guard.shutil.disk_usage',side_effect=OSError(str(self.root))):
            result=status(self.config,self.root)
            self.assertFalse(result['storage_ready']);self.assertNotIn(str(self.root),str(result))

    def test_readiness_reports_storage_and_existing_unknown_upload_is_unchanged(self):
        task=self.task();self.queue.update(task['id'],'needs_review');before=self.queue.get(task['id']);self.free=9
        result=readiness(self.config,self.root)
        self.assertFalse(result['storage_ready'])
        self.assertIsNone(process_one(self.queue,self.config,FakeUploader()))
        self.assertEqual(self.queue.get(task['id']),before)

    def test_provider_limit_increases_reservation_instead_of_silently_lowering_it(self):
        config={**self.config,'zhiling':{'enabled':True,'max_bytes':2000}}
        self.assertEqual(budget(config,'wechat'),2000)
        self.free=1500
        with self.assertRaises(StorageError):check_free(self.root,config,budget(config,'wechat'))


if __name__=='__main__':unittest.main()
