"""Headless sign-in tests use only synthetic clients, local files and a fake clock."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import httpx

from quark_login_cli import main, run_login, write_qr
from quark_web import WebClient, WebError, save_private


COOKIE = {'name': 'synthetic-cookie', 'value': 'synthetic-secret-value',
          'domain': '.quark.cn', 'path': '/'}
QR_TOKEN = 'synthetic-qr-token'
QR_URL = 'https://offline.invalid/qr?token=' + QR_TOKEN


class FakeClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


class FakeClient:
    def __init__(self, path, *, load_session, mode='success', clock=None):
        self.path = Path(path)
        self.load_session = load_session
        self.mode = mode
        self.clock = clock
        self.starts = self.polls = self.closes = 0
        self.http = SimpleNamespace(close=self.close)

    def close(self):
        self.closes += 1

    def qr_start(self):
        self.starts += 1
        return QR_TOKEN, QR_URL

    def qr_poll(self, token):
        assert token == QR_TOKEN
        self.polls += 1
        if self.mode == 'waiting':
            return False
        if self.mode == 'no-session':
            return True
        save_private(self.path, {'cookies': [COOKIE]})
        if self.mode == 'partial-failure':
            raise WebError(QR_URL + COOKIE['value'])
        if self.mode == 'cancel':
            raise KeyboardInterrupt()
        if self.mode == 'late-success':
            self.clock.now += 20
        if self.mode == 'invalid-cookie':
            save_private(self.path, {'cookies': [{'domain': 'attacker.invalid', 'name': 'x', 'value': 'x'}]})
        if self.mode == 'empty-cookie':
            save_private(self.path, {'cookies': [{}]})
        return True


class LoginCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = self.root / 'private' / 'session.json'
        self.session.parent.mkdir()
        self.original = b'old session bytes may be corrupted'
        self.session.write_bytes(self.original)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'categories': {'二创': 'offline'},
            'quark_web': {'session_file': str(self.session)}}), encoding='utf-8')
        self.clock = FakeClock()
        self.messages = []
        self.clients = []
        self.qr_paths = []
        for target in ('socket.socket.connect', 'socket.socket.connect_ex',
                       'httpx.Client.send', 'httpx.request'):
            blocker = patch(target, side_effect=AssertionError('Real network forbidden'))
            blocker.start(); self.addCleanup(blocker.stop)

    def factory(self, mode='success'):
        def create(path, *, load_session):
            self.assertNotEqual(Path(path), self.session)
            client = FakeClient(path, load_session=load_session, mode=mode, clock=self.clock)
            self.clients.append(client)
            return client
        return create

    def qr(self, value, path):
        self.assertEqual(value, QR_URL)
        self.assertEqual(path.parent, self.session.parent)
        self.qr_paths.append(path)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(b'synthetic QR')

    def run_login(self, mode='success', **kwargs):
        options = dict(start_login=True, timeout=10, refresh_after=3, poll_interval=1,
            client_factory=self.factory(mode), qr_writer=self.qr,
            clock=self.clock, wait=self.clock.wait, emit=self.messages.append)
        options.update(kwargs)
        return run_login(self.config, **options)

    def assert_clean(self):
        self.assertEqual(list(self.session.parent.glob('.quark-login-*')), [])
        for client in self.clients:
            self.assertEqual(client.closes, 1)
            self.assertFalse(client.load_session)
        output = '\n'.join(self.messages)
        for secret in (QR_TOKEN, QR_URL, COOKIE['name'], COOKIE['value']):
            self.assertNotIn(secret, output)

    def test_explicit_start_required_before_configuration_or_client_access(self):
        self.config.unlink()
        self.assertEqual(self.run_login(start_login=False), 'not_started')
        self.assertEqual(self.clients, [])
        self.assertEqual(self.session.read_bytes(), self.original)
        self.assert_clean()

    def test_cli_requires_start_flag_and_does_not_trigger_other_work(self):
        with patch('quark_login_cli.WebClient', side_effect=AssertionError('No login without flag')):
            result = main(['--config', str(self.config)], emit=self.messages.append,
                          client_factory=self.factory())
        self.assertEqual(result, 2)
        self.assertEqual(self.clients, [])
        self.assert_clean()

    def test_success_replaces_damaged_session_only_after_confirmation(self):
        def qr(value, path):
            self.assertEqual(self.session.read_bytes(), self.original)
            self.qr(value, path)
        self.assertEqual(self.run_login(qr_writer=qr), 'signed_in')
        self.assertEqual(json.loads(self.session.read_text())['cookies'], [COOKIE])
        self.assertEqual(self.clients[0].polls, 1)
        if os.name != 'nt':
            self.assertEqual(self.session.stat().st_mode & 0o777, 0o600)
        self.assert_clean()

    def test_timeout_refreshes_qr_with_bounded_total_wait_and_preserves_old_bytes(self):
        self.assertEqual(self.run_login('waiting', timeout=7), 'timed_out')
        self.assertEqual(self.clock.now, 7)
        self.assertEqual(self.clients[0].starts, 3)
        self.assertEqual(sum('二维码已刷新' in text for text in self.messages), 2)
        self.assertEqual(self.session.read_bytes(), self.original)
        self.assert_clean()

    def test_partial_session_failure_and_cancellation_keep_original_session(self):
        for mode, status in (('partial-failure', 'failed'), ('cancel', 'cancelled')):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_login(mode), status)
                self.assertEqual(self.session.read_bytes(), self.original)
                self.assert_clean()

    def test_png_generation_failure_preserves_old_session_and_removes_partial_png(self):
        def failure(value, path):
            self.qr(value, path)
            raise RuntimeError(QR_URL)
        self.assertEqual(self.run_login(qr_writer=failure), 'failed')
        self.assertEqual(self.clients[0].polls, 0)
        self.assertEqual(self.session.read_bytes(), self.original)
        self.assert_clean()

    def test_success_without_valid_staged_cookie_record_is_rejected(self):
        for mode in ('no-session', 'invalid-cookie', 'empty-cookie'):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_login(mode), 'failed')
                self.assertEqual(self.session.read_bytes(), self.original)
                self.assert_clean()

    def test_png_cleanup_failure_does_not_turn_committed_login_into_failure(self):
        original_unlink = Path.unlink
        def cannot_remove(path, *args, **kwargs):
            if path.suffix == '.png' and path.exists():
                raise OSError('synthetic cleanup failure')
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', cannot_remove):
            self.assertEqual(self.run_login(), 'signed_in')
        self.assertEqual(json.loads(self.session.read_text())['cookies'], [COOKIE])
        self.assertTrue(any('未能自动删除' in value for value in self.messages))
        for path in self.qr_paths:
            path.unlink(missing_ok=True)
        self.assert_clean()

    def test_login_returning_after_deadline_does_not_replace_old_session(self):
        self.assertEqual(self.run_login('late-success'), 'timed_out')
        self.assertEqual(self.session.read_bytes(), self.original)
        self.assert_clean()

    def test_cancel_before_start_does_not_construct_client(self):
        self.assertEqual(self.run_login(cancelled=lambda: True), 'cancelled')
        self.assertEqual(self.clients, [])
        self.assert_clean()

    def test_each_internal_request_obeys_remaining_deadline(self):
        observed = []
        class SeveralRequests(FakeClient):
            def request_json(inner, *args, **kwargs):
                observed.append(kwargs['timeout'])
                self.clock.now += 2
                return {}
            def qr_start(inner):
                inner.request_json('GET', 'https://offline.invalid/start')
                return super().qr_start()
            def qr_poll(inner, token):
                for _ in range(5):
                    inner.request_json('GET', 'https://offline.invalid/poll')
                return super().qr_poll(token)
        def factory(path, *, load_session):
            client = SeveralRequests(path, load_session=load_session)
            self.clients.append(client)
            return client
        self.assertEqual(self.run_login(timeout=5, client_factory=factory), 'timed_out')
        self.assertEqual(observed, [5, 2])
        self.assertEqual(self.session.read_bytes(), self.original)
        self.assert_clean()

    def test_new_session_created_only_on_success(self):
        self.session.unlink()
        self.assertEqual(self.run_login('waiting', timeout=2), 'timed_out')
        self.assertFalse(self.session.exists())
        self.assertEqual(self.run_login(), 'signed_in')
        self.assertTrue(self.session.is_file())
        self.assert_clean()

    def test_invalid_timeout_stops_before_client_creation(self):
        for timeout in (0, 901, True, float('inf'), float('nan')):
            self.assertEqual(self.run_login(timeout=timeout), 'failed')
        self.assertEqual(self.clients, [])
        self.assert_clean()

    def test_actual_qr_writer_creates_private_png_without_network(self):
        path = self.root / 'qr.png'
        write_qr(QR_URL, path)
        self.assertTrue(path.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
        if os.name != 'nt':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_web_client_can_skip_corrupt_session_and_default_still_loads_it(self):
        with httpx.Client(transport=httpx.MockTransport(lambda request: self.fail('No requests'))) as http:
            with self.assertRaises(WebError):
                WebClient(self.session, client=http)
            fresh = WebClient(self.session, client=http, load_session=False)
            self.assertEqual(len(fresh.http.cookies), 0)
            self.assertEqual(self.session.read_bytes(), self.original)
            fresh.close()
        save_private(self.session, {'cookies': [COOKIE]})
        with httpx.Client(transport=httpx.MockTransport(lambda request: self.fail('No requests'))) as http:
            existing = WebClient(self.session, client=http)
            self.assertEqual(len(existing.http.cookies), 1)
            existing.close()


if __name__ == '__main__':
    unittest.main()
