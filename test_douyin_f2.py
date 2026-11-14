import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from douyin_f2 import DownloadError, checksum, download_media, media_allowed, select_media, verify_file


class F2AdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / 'download.mp4'
        self.video = b'\x00\x00\x00\x18ftypmp42' + b'0' * 100
        self.media = {'content_id':'123456789012345', 'urls':['https://v.test.zjcdn.com/video.mp4'],
                      'bytes':len(self.video), 'resource_id':'video-resource-1',
                      'duration_seconds':5.0, 'width':1920, 'height':1080}
        self.probe = {'format':{'duration':'5.0','size':str(len(self.video))},
                      'streams':[{'codec_type':'video','codec_name':'h264','width':1920,'height':1080}]}

    def tearDown(self):
        self.temp.cleanup()

    def response(self):
        return {'status_code':0, 'aweme_detail':{'aweme_id':self.media['content_id'],
                'video':{'duration':5000,'play_addr':{'width':1920,'height':1080,
                'url_list':self.media['urls'], 'data_size':len(self.video),'uri':'video-resource-1'}}}}

    def fake_ydl(self, payload=None):
        payload = self.video if payload is None else payload
        class FakeYDL:
            def __init__(self, options): self.options = options
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def download(self, urls):
                Path(self.options['outtmpl']).write_bytes(payload)
                return 0
        return types.SimpleNamespace(YoutubeDL=FakeYDL)

    def probe_result(self):
        return subprocess.CompletedProcess([],0,stdout=json.dumps(self.probe),stderr='')

    def test_matching_video_selects_only_allowed_https_media(self):
        response = self.response()
        response['aweme_detail']['video']['play_addr']['url_list'] += ['http://127.0.0.1/private', 'https://evilzjcdn.com/x']
        selected = select_media(response,self.media['content_id'])
        self.assertEqual(selected['urls'], ['https://v.test.zjcdn.com/video.mp4'])
        for url in ['https://zjcdn.com.evil.example/x','https://user:secret@zjcdn.com/x','https://zjcdn.com:444/x','file:///a.mp4','https://zjcdn.com:bad/x']:
            self.assertFalse(media_allowed(url))

    def test_wrong_id_image_post_failed_status_and_oversize_rejected(self):
        with self.assertRaises(DownloadError): select_media(self.response(),'wrong')
        response=self.response(); response['aweme_detail']['images']=[{}]
        with self.assertRaises(DownloadError): select_media(response,self.media['content_id'])
        response=self.response(); response['status_code']=1
        with self.assertRaises(DownloadError): select_media(response,self.media['content_id'])
        with self.assertRaises(DownloadError): select_media(self.response(),self.media['content_id'],1)

    def test_partial_transfer_never_publishes_final_file(self):
        with patch.dict('sys.modules',{'yt_dlp':self.fake_ydl(self.video[:20])}):
            with self.assertRaises(DownloadError): download_media(self.media,self.output)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_suffix('.receipt.json').exists())

    def test_ffprobe_failure_blocks_publication(self):
        with patch.dict('sys.modules',{'yt_dlp':self.fake_ydl()}), patch('douyin_f2.subprocess.run',side_effect=FileNotFoundError):
            with self.assertRaises(DownloadError): download_media(self.media,self.output)
        self.assertFalse(self.output.exists())

    def test_same_size_html_and_wrong_duration_or_resolution_are_rejected(self):
        self.output.write_bytes(b'<' + b'x' * (len(self.video)-1))
        with self.assertRaises(DownloadError): verify_file(self.output,self.media)
        self.output.write_bytes(self.video)
        self.probe['format']['duration']='20'
        with patch('douyin_f2.subprocess.run',return_value=self.probe_result()):
            with self.assertRaises(DownloadError): verify_file(self.output,self.media)
        self.probe['format']['duration']='5'; self.probe['streams'][0]['width']=640
        with patch('douyin_f2.subprocess.run',return_value=self.probe_result()):
            with self.assertRaises(DownloadError): verify_file(self.output,self.media)

    def test_complete_file_has_receipt_and_retry_reuses_only_verified_file(self):
        with patch.dict('sys.modules',{'yt_dlp':self.fake_ydl()}), patch('douyin_f2.subprocess.run',return_value=self.probe_result()):
            first=download_media(self.media,self.output)
            self.assertEqual(first['sha256'],checksum(self.output))
            with patch.dict('sys.modules',{'yt_dlp':types.SimpleNamespace(YoutubeDL=lambda *_:self.fail('should not transfer again'))}):
                second=download_media(self.media,self.output)
            self.assertEqual(first,second)
        self.output.write_bytes(self.video[:-1]+b'1')
        with patch.dict('sys.modules',{'yt_dlp':self.fake_ydl()}):
            with self.assertRaises(DownloadError): download_media(self.media,self.output)
        self.assertEqual(self.output.read_bytes(),self.video[:-1]+b'1')

    def test_existing_unrelated_file_is_not_overwritten(self):
        self.output.write_bytes(b'user file')
        with patch.dict('sys.modules',{'yt_dlp':self.fake_ydl()}):
            with self.assertRaises(DownloadError): download_media(self.media,self.output)
        self.assertEqual(self.output.read_bytes(),b'user file')

    def test_identity_change_discards_only_owned_partial(self):
        stage=self.root/'download.stage.mp4'
        part=Path(str(stage)+'.part'); part.write_bytes(b'stale bytes')
        keep=self.root/'other.part'; keep.write_bytes(b'keep')
        self.output.with_suffix('.transfer.json').write_text('{}')
        with patch.dict('sys.modules',{'yt_dlp':self.fake_ydl()}), patch('douyin_f2.subprocess.run',return_value=self.probe_result()):
            download_media(self.media,self.output)
        self.assertFalse(part.exists())
        self.assertEqual(keep.read_bytes(),b'keep')

    def test_optional_aria2_keeps_validation_and_four_connection_limit(self):
        captured = []
        fake = self.fake_ydl()
        base = fake.YoutubeDL
        class Capture(base):
            def __init__(self, options):
                captured.append(options)
                super().__init__(options)
        fake.YoutubeDL = Capture
        with patch.dict('sys.modules', {'yt_dlp':fake}), patch('douyin_f2.subprocess.run',return_value=self.probe_result()):
            receipt = download_media(self.media,self.output,aria2='aria2c')
        self.assertEqual(captured[0]['external_downloader'], {'default':'aria2c'})
        self.assertIn('-x4',captured[0]['external_downloader_args']['aria2c'])
        self.assertEqual(receipt['sha256'],checksum(self.output))


if __name__=='__main__':
    unittest.main()
