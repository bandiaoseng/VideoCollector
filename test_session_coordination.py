"""Session races, failed writes and GUI bridge with synthetic credentials only."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from quark_web import WebClient, WebError, SessionBusy, save_private
from quark_login_cli import run_login
from quark_login import run_desktop_login
from test_quark_login_cli import COOKIE, FakeClient, FakeClock


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'session.json'
        save_private(self.path, {'cookies': [COOKIE]})
        self.original = self.path.read_bytes()
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'categories': {}, 'quark_web': {'session_file': str(self.path)}}))
        for name in ('socket.socket.connect', 'socket.socket.connect_ex'):
            guard = patch(name, side_effect=AssertionError('External network forbidden'))
            guard.start(); self.addCleanup(guard.stop)

    def client(self):
        http = httpx.Client(transport=httpx.MockTransport(lambda request: self.fail('No HTTP expected')))
        try:
            client = WebClient(self.path, client=http)
        except BaseException:
            http.close()
            raise
        self.addCleanup(client.close)
        return client

    def login(self, **kwargs):
        clock = FakeClock()
        options = dict(start_login=True, clock=clock, wait=clock.wait,
                       client_factory=FakeClient, qr_writer=lambda *args: None, emit=lambda _: None)
        options.update(kwargs)
        return run_login(self.config, **options)

    def test_second_client_blocked_before_transport_creation_and_release_is_idempotent(self):
        first = self.client()
        with patch('quark_web.http_client', side_effect=AssertionError('No second transport')):
            with self.assertRaises(SessionBusy): WebClient(self.path)
        first.close(); first.close()
        second = self.client()
        second.save()
        with self.assertRaises(WebError): first.save()

    def test_failed_constructor_and_transport_close_release_session(self):
        self.path.write_text('broken')
        with self.assertRaises(WebError): self.client()
        save_private(self.path, {'cookies': [COOKIE]})
        client = self.client()
        with patch.object(client.http, 'close', side_effect=OSError('synthetic cleanup error')):
            with self.assertRaises(OSError): client.close()
        client.http.close()
        self.client()

    def test_login_during_upload_does_not_start_qr_or_replace_session(self):
        self.client()
        status = self.login(client_factory=lambda *a, **k: self.fail('No QR client while busy'))
        self.assertEqual(status, 'busy')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(list(self.root.glob('.quark-login-*')))

    def test_login_holds_destination_until_staged_session_commits(self):
        def qr(*args):
            self.assertEqual(self.path.read_bytes(), self.original)
            with self.assertRaises(SessionBusy): self.client()
        self.assertEqual(self.login(qr_writer=qr), 'signed_in')
        self.client()

    def test_real_staging_client_closed_before_temporary_directory_removal(self):
        def create(path, **kwargs):
            http = httpx.Client(transport=httpx.MockTransport(lambda request: self.fail('No actual HTTP')))
            client = WebClient(path, client=http, **kwargs)
            client.qr_start = lambda: ('synthetic-token', 'synthetic-url')
            def poll(token):
                client.http.cookies.set(COOKIE['name'], COOKIE['value'], domain=COOKIE['domain'])
                client.save()
                return True
            client.qr_poll = poll
            return client
        self.assertEqual(self.login(client_factory=create), 'signed_in')
        self.assertFalse(list(self.root.glob('.quark-login-*')))
        self.client()

    def test_concurrent_private_writes_leave_one_complete_document_and_no_temps(self):
        def write(i): save_private(self.root / 'receipt.json', {'value': str(i) * 10000})
        with ThreadPoolExecutor(max_workers=4) as pool: list(pool.map(write, range(12)))
        saved = json.loads((self.root / 'receipt.json').read_text())
        self.assertIn(saved, [{'value': str(i) * 10000} for i in range(12)])
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_failed_serialization_preserves_old_record_and_removes_temp(self):
        with self.assertRaises(TypeError): save_private(self.path, {'invalid': object()})
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_desktop_bridge_uses_staging_and_hides_private_qr_path(self):
        inbox = queue.Queue(); stopped = threading.Event(); status_file = self.root / 'status.json'
        def login(path, **kwargs):
            clock = FakeClock()
            kwargs.update(client_factory=FakeClient, clock=clock, wait=clock.wait)
            return run_login(path, **kwargs)
        result = run_desktop_login(self.config, status_file, inbox, stopped, login=login)
        self.assertEqual(result, 'signed_in')
        self.assertEqual(json.loads(status_file.read_text())['state'], 'signed_in')
        rows = list(inbox.queue)
        self.assertEqual(sum(kind == 'qr' for kind, value in rows), 1)
        text = '\n'.join(value for kind, value in rows if kind == 'status')
        for private in ('synthetic-secret', 'synthetic-qr', str(self.root)):
            self.assertNotIn(private, text)

    def test_desktop_cancel_preserves_original_before_any_qr_request(self):
        inbox = queue.Queue(); stopped = threading.Event(); stopped.set()
        def login(path, **kwargs):
            kwargs['client_factory'] = lambda *a, **k: self.fail('No QR on cancellation')
            return run_login(path, **kwargs)
        result = run_desktop_login(self.config, self.root/'status.json', inbox, stopped, login=login)
        self.assertEqual(result, 'cancelled')
        self.assertEqual(self.path.read_bytes(), self.original)


if __name__ == '__main__': unittest.main()
