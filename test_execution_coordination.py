"""Cross-entry protection with temporary queues and no service operations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from collector import Queue, prepare, sha256
from desktop_app import run_request
from execution_coordination import execution_entry
from worker import run
from test_runtime_integration import OneCycleStop
from test_upload_evidence import receipt_rows, write_receipt


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.path=self.base/'config.json'
        self.config={'categories':{'二创':'folder'},'data_dir':'data'}
        self.path.write_text(json.dumps(self.config),encoding='utf-8')
        self.desktop=self.base/'.runtime'/'desktop-jobs';self.desktop.mkdir(parents=True)
        self.data=self.base/'data'
        self.queues=[];self.addCleanup(lambda:[q.db.close() for q in self.queues])
        for target in ('socket.socket.connect','socket.socket.connect_ex','collector.run_managed'):
            guard=patch(target,side_effect=AssertionError('No network/download'))
            guard.start();self.addCleanup(guard.stop)

    def queue(self,path):
        q=Queue(path);self.queues.append(q);return q

    def task(self,q,state='needs_review'):
        ident=q.submit_note('https://v.douyin.com/offline/','待核对 #二创',self.config['categories'])
        q.update(ident,state);return ident

    def test_running_worker_blocks_desktop_before_any_provider_construction(self):
        self.queue(self.data)
        with execution_entry(self.path,self.config,'worker',self.data):
            with self.assertRaises(RuntimeError),patch('desktop_app.ProgressUploader',side_effect=AssertionError('No uploader')):
                run_request(self.path,'https://v.douyin.com/new/','新点评 #二创')

    def test_desktop_blocks_worker_processing(self):
        with execution_entry(self.path,self.config,'desktop',self.desktop):
            with self.assertRaises(RuntimeError):
                run(self.path,OneCycleStop(),process=lambda *a:self.fail('No work while desktop holds lock'))

    def test_historical_desktop_unknown_upload_blocks_first_worker_launch(self):
        q=self.queue(self.desktop/('a'*64));ident=self.task(q)
        before=q.get(ident)
        with self.assertRaisesRegex(ValueError,'核对'):
            run(self.path,OneCycleStop(),process=lambda *a:self.fail('Unknown desktop upload ignored'))
        self.assertEqual(q.get(ident),before)

    def test_historical_web_unknown_upload_blocks_desktop_switch(self):
        q=self.queue(self.data);ident=self.task(q,'uploading')
        with self.assertRaisesRegex(ValueError,'核对'):
            run_request(self.path,'https://v.douyin.com/new/','新点评 #二创',uploader_factory=lambda *a:self.fail('No uploader'))
        self.assertEqual(q.get(ident)['state'],'uploading')

    def test_changed_data_directory_cannot_hide_registered_unknown_queue(self):
        old=self.queue(self.data)
        with execution_entry(self.path,self.config,'worker',self.data):self.task(old)
        changed={**self.config,'data_dir':'another'}
        new=self.base/'another';self.queue(new)
        with self.assertRaisesRegex(ValueError,'核对'):
            with execution_entry(self.path,changed,'worker',new):self.fail('Lost old queue')

    def test_completed_task_does_not_block_switch_and_no_queue_is_modified(self):
        q=self.queue(self.data);ident=self.task(q,'succeeded')
        before=q.get(ident)
        with execution_entry(self.path,self.config,'desktop',self.desktop):pass
        self.assertEqual(q.get(ident),before)

    def test_complete_receipt_can_clear_switch_barrier_without_mutating_original_task(self):
        q=self.queue(self.data)
        fixture=self.base/'fixture.mp4';fixture.write_bytes(b'\0\0\0\x18ftypmp42'+b'x'*100)
        ident=q.submit_note(str(fixture),'回执完成 #二创',self.config['categories'])
        task=q.claim();local=prepare(task,q.root,{})
        task=q.start_upload(task,local,sha256(local));q.update(ident,'needs_review')
        write_receipt(q.root,task,receipt_rows(task))
        with execution_entry(self.path,self.config,'desktop',self.desktop):pass
        self.assertEqual(q.get(ident)['state'],'needs_review')

    def test_lock_releases_after_child_process_is_terminated(self):
        self.queue(self.data)
        script='import sys;sys.path.insert(0,sys.argv[2]);from execution_coordination import execution_entry; import json; from pathlib import Path; p=Path(sys.argv[1]); c=json.loads(p.read_text());\nwith execution_entry(p,c,"worker",p.parent/"data"):\n print("ready",flush=True);sys.stdin.read()'
        # Windows venv python.exe is a launcher; terminate the actual interpreter
        # so this test measures OS lock release rather than launcher lifetime.
        executable=getattr(sys,'_base_executable',sys.executable)
        import sysconfig
        child=subprocess.Popen([executable,'-c',script,str(self.path),sysconfig.get_path('purelib')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            self.assertEqual(child.stdout.readline().strip(),'ready')
            with self.assertRaises(RuntimeError):
                with execution_entry(self.path,self.config,'desktop',self.desktop):pass
            child.terminate();child.wait(timeout=5)
            with execution_entry(self.path,self.config,'desktop',self.desktop):pass
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=5)
            for stream in (child.stdin,child.stdout,child.stderr):stream.close()


if __name__=='__main__':unittest.main()
