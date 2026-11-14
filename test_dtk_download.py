import io
from pathlib import Path
import tempfile
import unittest

from dtk_download import Client, DtkError, download, select_video, wait_for


MEDIA = b'\x00\x00\x00\x18ftypmp42' + bytes(40)


class FakeClient:
    base = 'https://self-hosted.example'
    def __init__(self):
        self.calls = []
        self.transfer = MEDIA
        self.state = 'done'

    def json(self, route, body=None):
        self.calls.append((route, body))
        if route == '/api/v1/parse':
            return {'task_id': 'parse-1', 'state': 'queued'}
        if route == '/api/v1/tasks/parse-1':
            return {'state': 'done', 'data': {'platform': 'douyin', 'kind': 'video', 'content_id': 'video-1'}}
        if route == '/api/v1/downloads':
            return {'download_id': 'download-1'}
        if route == '/api/v1/downloads/download-1':
            return {'state': self.state, 'on_disk': True, 'files': [
                {'name': 'video.mp4', 'kind': 'video', 'state': 'done', 'bytes': len(MEDIA)}]}
        raise AssertionError(route)

    def open(self, route):
        self.asserted_route = route
        return io.BytesIO(self.transfer)


class DtkTests(unittest.TestCase):
    def test_uses_upstream_download_service_and_resumes_same_task(self):
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient()
            output = Path(folder) / 'download.mp4'
            download({}, 'https://v.douyin.com/sample/', output, client=client)
            self.assertEqual(output.read_bytes(), MEDIA)
            self.assertEqual(client.asserted_route, '/api/v1/downloads/download-1/files/video.mp4')
            output.unlink()
            download({}, 'https://v.douyin.com/sample/', output, client=client)
            posts = [route for route, body in client.calls if body is not None]
            self.assertEqual(posts, ['/api/v1/parse', '/api/v1/downloads'])

    def test_incomplete_transfer_never_publishes_output(self):
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient()
            client.transfer = MEDIA[:10]
            output = Path(folder) / 'download.mp4'
            with self.assertRaises(DtkError):
                download({}, 'https://v.douyin.com/sample/', output, client=client)
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix('.transfer-part').exists())
            self.assertTrue(output.with_suffix('.dtk.json').exists())

    def test_failure_inside_success_envelope_is_not_success(self):
        with self.assertRaises(DtkError):
            wait_for(lambda: {'state': 'failed', 'error': {'code': 'UPSTREAM_RISK_CONTROL'}})

    def test_observation_timeout_does_not_resubmit(self):
        calls = []
        times = iter([0, 2])
        def read():
            calls.append('read')
            return {'state': 'running'}
        with self.assertRaises(DtkError):
            wait_for(read, timeout=1, clock=lambda: next(times), sleep=lambda _: None)
        self.assertEqual(calls, ['read'])

    def test_demo_cannot_be_production_backend(self):
        with self.assertRaises(DtkError):
            Client({'base_url': 'https://demo.douyin.wtf'})

    def test_partial_or_missing_files_are_rejected(self):
        for record in ({'state': 'partial', 'on_disk': True}, {'state': 'done', 'on_disk': False},
                       {'state': 'done', 'on_disk': True, 'files': []}):
            with self.subTest(record=record), self.assertRaises(DtkError):
                select_video(record)


if __name__ == '__main__':
    unittest.main()
