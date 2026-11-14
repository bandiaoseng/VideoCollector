"""Local cancellation/race tests; no platform or cloud requests."""
import concurrent.futures
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from collector import Queue, prepare, process_one, sha256
from test_collector import FakeUploader
from test_process_runner import is_running


class CancellationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.queue = Queue(self.root / 'data')
        self.source = self.root / 'source.mp4'
        self.source.write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'x' * 128)
        self.task_id = self.queue.submit(str(self.source), '保留点评', '二创', {'二创': 'offline-folder'})

    def tearDown(self):
        self.queue.db.close()
        self.temp.cleanup()

    def test_cancel_before_work_retains_note_and_never_claims_or_retries(self):
        self.assertEqual(self.queue.cancel(self.task_id)['state'], 'cancelled')
        self.assertEqual(self.queue.cancel(self.task_id)['state'], 'cancelled')
        self.assertIsNone(process_one(self.queue, {}, FakeUploader()))
        self.queue.recover()
        self.assertEqual(self.queue.get(self.task_id)['comment'], '保留点评')
        with self.assertRaises(ValueError):
            self.queue.retry(self.task_id)
        self.assertTrue(self.source.is_file())

    def test_cancel_after_prepare_prevents_upload_and_late_error_cannot_resurrect_task(self):
        for fail in (False, True):
            task_id = self.task_id if not fail else self.queue.submit(str(self.source), '第二条', '二创', {'二创': 'offline-folder'})
            def interrupted(*args, **kwargs):
                local = prepare(*args, **kwargs)
                self.assertEqual(self.queue.cancel(task_id)['state'], 'cancel_requested')
                if fail:
                    raise OSError('late downloader error')
                return local
            uploader = FakeUploader()
            with patch('collector.prepare', side_effect=interrupted):
                result = process_one(self.queue, {}, uploader)
            self.assertEqual(result['state'], 'cancelled')
            self.assertEqual(uploader.calls, [])
            self.queue.recover()
            self.assertEqual(self.queue.get(task_id)['state'], 'cancelled')

    def test_cancel_during_preflight_failure_is_not_changed_to_setup_required(self):
        queue, task_id = self.queue, self.task_id
        class Uploader(FakeUploader):
            def preflight(self):
                queue.cancel(task_id)
                raise OSError('offline setup error')
        self.assertEqual(process_one(queue, {}, Uploader())['state'], 'cancelled')

    def test_running_download_process_stops_without_upload(self):
        self.queue.cancel(self.task_id)
        task_id = self.queue.submit('https://v.douyin.com/offline/', '正在下载的点评', '二创', {'二创': 'offline-folder'})
        marker = self.root / 'started.pid'
        script = self.root / 'slow.py'
        script.write_text('import os, time\nfrom pathlib import Path\n' +
                          f'Path({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(5)\n', encoding='utf-8')
        config = {'download_commands': {'douyin': [sys.executable, str(script), '{output}']}, 'download_timeout': 8}
        uploader = FakeUploader()
        def work():
            queue = Queue(self.queue.root)
            try:
                return process_one(queue, config, uploader)
            finally:
                queue.db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(work)
            deadline = time.monotonic() + 4
            while not marker.exists() and not running.done() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), 'fixture must actually start before cancellation')
            self.assertEqual(self.queue.cancel(task_id)['state'], 'cancel_requested')
            self.assertEqual(running.result(timeout=3)['state'], 'cancelled')
        self.assertFalse(is_running(int(marker.read_text())))
        self.assertEqual(uploader.calls, [])

    def test_cancel_and_retry_race_never_resurrects_task(self):
        self.queue.update(self.task_id, 'prepare_failed')
        barrier = threading.Barrier(2)
        def action(name):
            queue = Queue(self.queue.root)
            try:
                barrier.wait(timeout=3)
                try:
                    getattr(queue, name)(self.task_id)
                except ValueError:
                    return 'refused'
                return 'accepted'
            finally:
                queue.db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(action, ('cancel', 'retry')))
        self.assertEqual(self.queue.get(self.task_id)['state'], 'cancelled')
        self.assertIsNone(self.queue.claim())

    def test_cancel_and_upload_gate_have_only_one_winner(self):
        task = self.queue.claim()
        local = prepare(task, self.queue.root, {})
        checksum = sha256(local)
        barrier = threading.Barrier(2)
        def action(name):
            queue = Queue(self.queue.root)
            try:
                barrier.wait(timeout=3)
                try:
                    if name == 'cancel':
                        queue.cancel(task['id'])
                    else:
                        queue.start_upload(task, local, checksum)
                    return True
                except ValueError:
                    return False
            finally:
                queue.db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            winners = list(pool.map(action, ('cancel', 'upload')))
        self.assertEqual(sum(winners), 1)
        self.assertEqual(self.queue.get(task['id'])['state'], 'cancel_requested' if winners[0] else 'uploading')

    def test_upload_phase_or_unknown_result_cannot_be_reported_cancelled(self):
        queue, task_id = self.queue, self.task_id
        class Uploader(FakeUploader):
            def upload(inner, path, task):
                with self.assertRaises(ValueError):
                    queue.cancel(task_id)
                return super().upload(path, task)
        self.assertEqual(process_one(queue, {}, Uploader())['state'], 'succeeded')
        for state in ('succeeded', 'needs_review', 'uploading'):
            queue.update(task_id, state)
            with self.assertRaises(ValueError):
                queue.cancel(task_id)
            self.assertEqual(queue.get(task_id)['state'], state)

    def test_restart_finishes_requested_cancellation_instead_of_requeuing(self):
        self.queue.claim()
        self.queue.cancel(self.task_id)
        self.queue.recover()
        self.assertEqual(self.queue.get(self.task_id)['state'], 'cancelled')
        self.assertIsNone(self.queue.claim())


if __name__ == '__main__':
    unittest.main()
