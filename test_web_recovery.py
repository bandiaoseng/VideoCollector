"""Authenticated mobile recovery routes use fake Quark listings only."""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_web
from collector import Queue, WorkerLock, prepare, sha256
from quark_web_uploader import WebUploader
from test_quark_recovery import FakeWeb


class WebRecoveryTests(unittest.TestCase):
    setUp = test_web.WebTest.setUp
    tearDown = test_web.WebTest.tearDown
    login = test_web.WebTest.login
    create = test_web.WebTest.create
    submit = test_web.WebTest.submit

    def fixture(self, hashes=True):
        self.draft = self.create()
        submitted = self.submit(self.draft).json()
        self.ident = submitted['task_id']
        session = self.root / 'session.json'
        session.write_text('{"cookies":[]}', encoding='utf-8')
        self.config['quark_web'] = {'session_file': str(session), 'category_paths': {'二创':['搜集视频','二创']}}
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')
        q = Queue(self.root / 'data')
        try:
            task = q.claim()
            source = self.root / 'fixture.mp4'
            source.write_bytes(b'\0\0\0\x18ftypmp42' + b'offline-video'*100)
            local = prepare({**task, 'source':str(source)}, q.root, {})
            receipt = local.parent / 'prepared.json'
            data = json.loads(receipt.read_text(encoding='utf-8'))
            data['task']['source'] = task['source']
            receipt.write_text(json.dumps(data), encoding='utf-8')
            self.web = FakeWeb()
            uploader = WebUploader(self.config, client=self.web)
            uploader.check_destination(local, task)
            q.start_upload(task, local, sha256(local))
            q.update(self.ident, 'needs_review')
            row = {'fid':'private-remote-id','file_name':local.name,'file':True,'size':local.stat().st_size}
            if hashes:
                row.update({k:hashlib.new(k,local.read_bytes()).hexdigest() for k in ('md5','sha1')})
            self.web.rows = [row]
        finally:
            q.db.close()
        for target, effect in [('quark_web_uploader.WebClient', lambda *a,**k:self.web),
                               ('collector.run_managed', AssertionError('No download')),
                               ('httpx.HTTPTransport.handle_request', AssertionError('No external HTTP'))]:
            guard=patch(target, side_effect=effect)
            guard.start();self.addCleanup(guard.stop)

    def review(self, **body):
        return self.client.post(f'/api/drafts/{self.draft["id"]}/review',json=body,headers=self.headers)

    def test_original_directory_hash_review_succeeds_even_while_worker_lock_held(self):
        self.fixture()
        with WorkerLock(self.root/'data'):
            response=self.review()
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['state'],'succeeded')
        count=len(self.web.list_calls)
        self.assertEqual(self.review().json()['state'],'succeeded')
        self.assertEqual(len(self.web.list_calls),count)
        self.assertEqual(self.web.upload_calls,0)
        self.assertNotIn('private-remote-id',response.text)
        self.assertNotIn(str(self.root),response.text)

    def test_missing_hash_requires_explicit_bound_confirmation(self):
        self.fixture(hashes=False)
        first=self.review().json()
        self.assertEqual(first['state'],'needs_review')
        self.assertNotIn('fid',first['review_candidate'])
        self.assertEqual(self.client.get(f'/api/drafts/{self.draft["id"]}').json()['state'],'needs_review')
        self.assertEqual(self.review(confirmation=first['review_confirmation']).json()['state'],'succeeded')
        self.assertEqual(self.web.upload_calls,0)

    def test_changed_candidate_rejects_old_confirmation(self):
        self.fixture(hashes=False)
        first=self.review().json()
        self.web.rows[0]['fid']='replacement'
        second=self.review(confirmation=first['review_confirmation']).json()
        self.assertEqual(second['state'],'needs_review')
        self.assertNotEqual(first['review_confirmation'],second['review_confirmation'])
        self.assertEqual(self.web.upload_calls,0)

    def test_busy_task_rejects_second_review_without_querying_cloud(self):
        self.fixture()
        folder=self.root/'data'/'review-locks'/self.ident
        folder.mkdir(parents=True)
        count=len(self.web.list_calls)
        with WorkerLock(folder):
            self.assertEqual(self.review().status_code,409)
        self.assertEqual(len(self.web.list_calls),count)

    def test_no_match_stays_unresolved_and_invalid_token_is_rejected(self):
        self.fixture()
        self.assertEqual(self.review(confirmation='bad').status_code,422)
        self.web.rows=[]
        result=self.review().json()
        self.assertEqual(result['state'],'needs_review')
        self.assertNotIn('review_confirmation',result)
        self.assertEqual(self.web.upload_calls,0)

    def test_unauthenticated_review_has_no_side_effect(self):
        self.fixture()
        self.client.cookies.clear()
        count=len(self.web.list_calls)
        self.assertEqual(self.review().status_code,401)
        self.assertEqual(len(self.web.list_calls),count)


if __name__=='__main__':unittest.main()
