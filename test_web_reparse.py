"""One-shot parser approval with synthetic network and persistent barriers."""
import json
import subprocess
import unittest
from unittest.mock import patch

import test_web
from collector import Queue, process_one
from parse_recovery import parser_recovery
from test_collector import FakeUploader
from test_zhiling_download import FakeClient, SOURCE
from zhiling_download import download, private_json


class WebReparseTests(unittest.TestCase):
    setUpBase = test_web.WebTest.setUp
    def tearDown(self):
        if hasattr(self,'q'):self.q.db.close()
        test_web.WebTest.tearDown(self)
    login = test_web.WebTest.login
    create = test_web.WebTest.create
    submit = test_web.WebTest.submit

    def setUp(self):
        self.setUpBase()
        self.ready['platforms']['wechat']=True
        self.draft=self.create(SOURCE)
        submitted=self.submit(self.draft).json()
        self.q=Queue(self.root/'data');self.addCleanup(self.q.db.close)
        self.ident=submitted['task_id']
        self.output=self.q.root/'files'/self.ident/'download.mp4'
        self.fake=FakeClient(b'\0\0\0\x18ftypmp42'+b'fake-video'*40)
        self.cfg={'zhiling':{'enabled':True}}
        for target, effect in [('zhiling_download.check_settings',None),
                               ('httpx.HTTPTransport.handle_request',AssertionError('No network')),
                               ('urllib.request.OpenerDirector.open',AssertionError('No network'))]:
            p=patch(target,side_effect=effect);p.start();self.addCleanup(p.stop)
        p=patch('zhiling_download.verify_file',return_value={'duration':1,'audio_present':True})
        p.start();self.addCleanup(p.stop)
        p=patch('collector.run_managed',side_effect=self.adapter)
        p.start();self.addCleanup(p.stop)
        self.fake.parse_failure=TimeoutError('offline timeout')
        self.assertEqual(self.run_task()['state'],'prepare_failed')
        self.token=self.recovery()['parse_retry_token']

    def adapter(self,args,**kwargs):
        try:download(self.cfg['zhiling'],SOURCE,self.output,client=self.fake)
        except Exception:
            return subprocess.CompletedProcess(args,1,json.dumps({'error':'offline failure'}),'')
        return subprocess.CompletedProcess(args,0,'','')

    def run_task(self):return process_one(self.q,self.cfg,FakeUploader())
    def recovery(self):return parser_recovery(self.output.parent,SOURCE)
    def approve(self,token=None,approved=True):
        return self.client.post(f'/api/drafts/{self.draft["id"]}/reparse',
            json={'attempt_token':token or self.token,'approved':approved},headers=self.headers)

    def test_explicit_approval_and_http_replay_execute_once(self):
        self.assertEqual(self.approve(approved=False).status_code,400)
        self.assertEqual(self.fake.parse_count,1)
        self.assertEqual(self.approve().status_code,200)
        self.assertEqual(self.approve().status_code,200)
        self.assertEqual(self.fake.parse_count,1)
        self.fake.parse_failure=None
        self.assertEqual(self.run_task()['state'],'succeeded')
        self.assertEqual(self.approve().json()['state'],'succeeded')
        self.assertIsNone(self.run_task())
        self.assertEqual(self.fake.parse_count,2)

    def test_second_failure_needs_new_approval_and_old_token_never_replays(self):
        self.approve();self.run_task()
        new_token=self.recovery()['parse_retry_token']
        self.assertNotEqual(new_token,self.token)
        self.assertEqual(self.approve().json()['state'],'prepare_failed')
        self.q.retry(self.ident);self.run_task()
        self.assertEqual(self.fake.parse_count,2)
        self.fake.parse_failure=None
        self.assertEqual(self.approve(new_token).status_code,200)
        self.assertEqual(self.run_task()['state'],'succeeded')
        self.assertEqual(self.fake.parse_count,3)

    def test_connection_failure_retains_barrier_and_pending_approval(self):
        self.approve()
        self.fake.connection_failure=TimeoutError('offline preflight')
        self.run_task()
        self.assertEqual(self.fake.parse_count,1)
        self.assertEqual(self.recovery()['parse_retry_token'],self.token)
        self.fake.connection_failure=None;self.fake.parse_failure=None
        self.q.retry(self.ident)
        self.assertEqual(self.run_task()['state'],'succeeded')
        self.assertEqual(self.fake.parse_count,2)

    def test_crash_after_new_barrier_cannot_consume_old_approval_after_restart(self):
        self.approve()
        class Crash(BaseException):pass
        def crash(path,data):
            private_json(path,data)
            if path.name=='download.zhiling-private.json':raise Crash()
        with patch('zhiling_download.private_json',side_effect=crash), self.assertRaises(Crash):
            self.run_task()
        self.q.db.close();self.q=Queue(self.root/'data');self.addCleanup(self.q.db.close)
        self.q.recover()
        self.run_task()
        self.assertEqual(self.fake.parse_count,1)
        self.assertNotEqual(self.recovery()['parse_retry_token'],self.token)

    def test_missing_barrier_with_approval_fails_without_request(self):
        self.approve()
        self.output.with_suffix('.zhiling-private.json').unlink()
        self.assertEqual(self.run_task()['state'],'prepare_failed')
        self.assertEqual(self.fake.parse_count,1)

    def test_restart_before_consumption_and_login_failure_do_not_charge(self):
        self.approve()
        self.q.db.close();self.q=Queue(self.root/'data');self.addCleanup(self.q.db.close)
        self.q.recover()
        bad=FakeUploader()
        with patch.object(bad,'preflight',side_effect=ValueError('offline login expired')):
            self.assertEqual(process_one(self.q,self.cfg,bad)['state'],'setup_required')
        self.assertEqual(self.fake.parse_count,1)
        self.fake.parse_failure=None;self.q.retry(self.ident)
        self.assertEqual(self.run_task()['state'],'succeeded')
        self.assertEqual(self.fake.parse_count,2)

    def test_invalid_confirmation_and_other_task_state_do_not_queue(self):
        self.assertEqual(self.approve('a'*64).status_code,400)
        self.client.cookies.clear()
        self.assertEqual(self.approve().status_code,401)
        self.assertEqual(self.q.get(self.ident)['state'],'prepare_failed')
        self.assertEqual(self.fake.parse_count,1)

    def test_legacy_barrier_without_attempt_id_gets_unique_new_identity(self):
        path=self.output.with_suffix('.zhiling-private.json')
        data=json.loads(path.read_text(encoding='utf-8'));data.pop('attempt_id')
        private_json(path,data)
        self.token=self.recovery()['parse_retry_token']
        self.approve();self.run_task()
        new=json.loads(path.read_text(encoding='utf-8'))
        self.assertTrue(new['attempt_id'])
        self.assertNotEqual(self.recovery()['parse_retry_token'],self.token)
        self.assertEqual(self.approve().json()['state'],'prepare_failed')
        self.assertEqual(self.fake.parse_count,2)


if __name__=='__main__':unittest.main()
