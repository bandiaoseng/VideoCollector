import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import httpx
from collector import Queue, prepare, process_one
from desktop_app import run_request
from quark_web import WebClient, WebError
from quark_web_uploader import WebUploader
from upload_evidence import confirmed_upload


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = self.root / 'session.json'
        self.session.write_text('{"cookies":[]}')
        for target in ('socket.socket.connect', 'socket.socket.connect_ex'):
            p = patch(target, side_effect=AssertionError('No network in tests'))
            p.start(); self.addCleanup(p.stop)

    def client(self, response):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=response(r)))
        http = httpx.Client(transport=transport)
        self.addCleanup(http.close)
        client = WebClient(self.session, client=http)
        self.addCleanup(client.close)
        return client

    def test_complete_pagination_and_unique_directory_mapping(self):
        def response(r):
            page = int(r.url.params['_page'])
            rows = [{'fid':str(i), 'file_name':str(i), 'file':True} for i in range(100)] if page == 1 else [
                {'fid':'folder', 'file_name':'二创', 'file':False}]
            return {'status':200, 'data':{'list':rows}, 'metadata':{'_total':101}}
        client = self.client(response)
        self.assertEqual(len(client.list_all('0')),101)
        self.assertEqual(client.resolve(['二创']),'folder')

    def test_missing_total_or_failed_login_cannot_mean_empty_folder(self):
        for result in ({'status':200,'data':{'list':[]}}, {'status':401,'code':31001}):
            client = self.client(lambda r:result)
            with self.assertRaises(WebError): client.list_all('0')
            client.close()

    def test_object_storage_host_is_checked_before_sending_file(self):
        client = self.client(lambda r:{'status':200})
        with self.assertRaises(WebError), patch('httpx.request', side_effect=AssertionError('No upload')):
            client.multipart(self.root/'unused',{'upload_url':'https://attacker.invalid',
                'bucket':'bucket','obj_key':'key','upload_id':'id'},4096,lambda _:None)

    def test_multipart_uses_separate_transport_and_checks_commit(self):
        client = self.client(lambda r:{'status':200,'data':{'auth_key':'synthetic-oss-auth'}})
        source=self.root/'fixture.mp4';source.write_bytes(b'x'*2100)
        info={'upload_url':'https://pds.quark.cn','bucket':'test','obj_key':'key',
              'upload_id':'id','task_id':'task','auth_info':'synthetic','callback':{}}
        calls=[]
        def send(method,url,**kw):
            calls.append((method,url,kw))
            self.assertNotIn('Cookie',kw['headers'])
            return httpx.Response(200,headers={'ETag':'"part-hash"'})
        with patch('httpx.request',side_effect=send):
            client.multipart(source,info,1024,lambda _:None)
        self.assertEqual([r[0] for r in calls],['PUT','PUT','PUT','POST'])
        self.assertEqual(sum(len(r[2]['content']) for r in calls[:-1]),2100)

    def test_independent_receipt_recovers_exact_file_but_rejects_tampering(self):
        source=self.root/'fixture.mp4';source.write_bytes(b'\0\0\0\x18ftypmp42'+b'x'*256)
        q=Queue(self.root/'queue');self.addCleanup(q.db.close)
        ident=q.submit_note(str(source),'点评 #二创',{'二创':'old-sdk-fid'})
        class FakeWeb:
            http=SimpleNamespace(close=lambda:None)
            done=False
            def list_all(self,parent):
                return [{'fid':'new-web-fid','file_name':'点评.mp4','size':source.stat().st_size,'file':True}] if self.done and parent=='target' else []
            def resolve(self,parts): return 'target'
            def upload(self,path,parent,progress):
                self.done=True
                return {k:hashlib.new(k,path.read_bytes()).hexdigest() for k in ['md5','sha1','sha256']}
        config={'quark_web':{'session_file':str(self.session),'category_paths':{'二创':['搜集视频','二创']}}}
        uploader=WebUploader(config,client=FakeWeb())
        report=process_one(q,config,uploader)
        self.assertEqual(report['state'],'succeeded')
        self.assertEqual(confirmed_upload(q.root,q.get(ident))['remote_fid'],'new-web-fid')
        q.update(ident,'needs_review');q.recover()
        self.assertEqual(q.get(ident)['state'],'succeeded')
        task=q.get(ident)
        p=q.root/'files'/ident/'upload-attempts'/task['upload_attempt']/'web-upload.json'
        data=json.loads(p.read_text(encoding='utf-8'));data['remote']['size']+=1
        p.write_text(json.dumps(data),encoding='utf-8')
        with self.assertRaises(ValueError):confirmed_upload(q.root,task)

    def test_cache_only_mode_never_downloads_when_file_is_missing(self):
        path=self.root/'config.json'
        path.write_text(json.dumps({'categories':{'二创':'original-fid'}}),encoding='utf-8')
        with patch('collector.prepare',side_effect=AssertionError('No download')),self.assertRaisesRegex(ValueError,'不会重新下载'):
            run_request(path,'https://weixin.qq.com/sph/test','点评 #二创',require_cached=True)


if __name__=='__main__':unittest.main()
