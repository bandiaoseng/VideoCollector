"""Real, network-free subprocess tests; all fixtures stop within seconds."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from process_runner import run_managed, ProcessCancelled


TREE_SCRIPT = '''
import os, subprocess, sys, time
from pathlib import Path
folder, role, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
(folder / (role + '.pid')).write_text(str(os.getpid()))
if role != 'leaf':
    child = 'child' if role == 'root' else 'leaf'
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    subprocess.Popen([sys.executable, __file__, str(folder), child, mode], **flags)
if role == 'root' and mode == 'exit':
    deadline = time.monotonic() + 4
    while not (folder / 'leaf.pid').exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    sys.exit(0 if (folder / 'leaf.pid').exists() else 7)
time.sleep(5)
'''


def is_running(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes as w
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        api.OpenProcess.restype = w.HANDLE
        api.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        api.WaitForSingleObject.restype = w.DWORD
        api.CloseHandle.argtypes = [w.HANDLE]
        api.CloseHandle.restype = w.BOOL
        handle = api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if not handle:
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: PID gone
                return False
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            result = api.WaitForSingleObject(handle, 0)
            if result == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            return result == 258  # WAIT_TIMEOUT
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f'/proc/{pid}/stat')
    try:
        if stat.exists() and stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
            return False
    except FileNotFoundError:
        return False
    return True


class ProcessRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='collector process ')
        self.root = Path(self.temp.name)
        self.script = self.root / 'tree.py'
        self.script.write_text(TREE_SCRIPT, encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def assert_tree_stopped(self):
        pids = []
        for role in ('root', 'child', 'leaf'):
            path = self.root / (role + '.pid')
            self.assertTrue(path.exists(), f'{role} never started; test did not cover whole tree')
            pids.append(int(path.read_text()))
        deadline = time.monotonic() + 1
        while any(is_running(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(any(is_running(pid) for pid in pids), 'task left running descendants')

    def test_timeout_stops_children_and_grandchildren_but_not_unrelated_process(self):
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'], **flags)
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                run_managed([sys.executable, str(self.script), str(self.root), 'root', 'hang'], timeout=2)
            self.assert_tree_stopped()
            self.assertIsNone(other.poll())
        finally:
            other.terminate()
            other.wait(timeout=5)

    def test_capture_timeout_does_not_hang_on_descendants_inheriting_output(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_managed([sys.executable, str(self.script), str(self.root), 'root', 'hang'],
                        timeout=2, capture_output=True)
        self.assertLess(time.monotonic() - started, 4.5)
        self.assert_tree_stopped()

    @unittest.skipUnless(os.name == 'nt', 'Windows process-handle exit barrier')
    def test_cancel_returns_after_existing_members_release_output(self):
        output = self.root / 'captured.ndjson'
        with self.assertRaises(ProcessCancelled):
            run_managed([sys.executable, str(self.script), str(self.root), 'root', 'hang'],
                        timeout=4, capture_output=True, stdout_path=output,
                        cancel_check=lambda: (self.root / 'leaf.pid').exists())
        # No grace-period polling: cancelled must not race open inherited handles.
        for role in ('root', 'child', 'leaf'):
            self.assertFalse(is_running(int((self.root / (role + '.pid')).read_text())))
        output.unlink()

    def test_requested_cancellation_stops_the_whole_process_tree(self):
        with self.assertRaises(ProcessCancelled):
            run_managed([sys.executable, str(self.script), str(self.root), 'root', 'hang'], timeout=4,
                        cancel_check=lambda: (self.root / 'leaf.pid').exists())
        self.assert_tree_stopped()

    def test_successful_parent_exit_cleans_remaining_descendants(self):
        result = run_managed([sys.executable, str(self.script), str(self.root), 'root', 'exit'],
                             timeout=4, check=True)
        self.assertEqual(result.returncode, 0)
        self.assert_tree_stopped()

    def test_large_output_and_nonzero_result_are_preserved(self):
        command = [sys.executable, '-c',
                   'import sys; sys.stdout.write("x" * 200000); sys.stderr.write("failed"); sys.exit(9)']
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            run_managed(command, timeout=5, capture_output=True, check=True)
        self.assertEqual(caught.exception.returncode, 9)
        self.assertEqual(caught.exception.stdout, 'x' * 200000)
        self.assertEqual(caught.exception.stderr, 'failed')

    def test_arguments_are_literal_and_not_shell_code(self):
        value = '点评 & echo unsafe; $(command) "quoted"'
        result = run_managed([sys.executable, '-c',
                              'import sys; sys.stdout.buffer.write(sys.argv[1].encode("utf-8"))', value],
                             timeout=5, capture_output=True, check=True)
        self.assertEqual(result.stdout, value)

    def test_invalid_timeout_cannot_start_an_unbounded_task(self):
        for timeout in (0, -1, float('inf'), float('nan'), True, '5'):
            with self.subTest(timeout=timeout), patch('process_runner.subprocess.Popen') as launch:
                with self.assertRaises(ValueError):
                    run_managed([sys.executable, '-c', 'pass'], timeout=timeout)
                launch.assert_not_called()

    def test_persistent_output_is_kept_and_never_overwritten(self):
        output = self.root / 'stdout.ndjson'
        run_managed([sys.executable, '-c', 'print("receipt")'], timeout=3,
                    capture_output=True, stdout_path=output, check=True)
        self.assertEqual(output.read_text().strip(), 'receipt')
        with patch('process_runner.subprocess.Popen') as launch:
            with self.assertRaises(FileExistsError):
                run_managed([sys.executable, '-c', 'print("replacement")'], timeout=3,
                            capture_output=True, stdout_path=output)
            launch.assert_not_called()
        self.assertEqual(output.read_text().strip(), 'receipt')


if __name__ == '__main__':
    unittest.main()
