"""Bound task subprocess lifetimes, including ordinary descendant processes.

Windows: a helper joins a kill-on-close Job before starting the real command.
POSIX: a fresh session provides a dedicated process group. Detached POSIX
daemons are outside this contract; configured downloaders must not daemonize.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid


def _windows_api():
    import ctypes
    from ctypes import wintypes as w
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateJobObjectW': ([w.LPVOID, w.LPCWSTR], w.HANDLE),
        'OpenJobObjectW': ([w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE),
        'SetInformationJobObject': ([w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD], w.BOOL),
        'AssignProcessToJobObject': ([w.HANDLE, w.HANDLE], w.BOOL),
        'QueryInformationJobObject': ([w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, w.LPVOID], w.BOOL),
        'OpenProcess': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        'IsProcessInJob': ([w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
        'WaitForSingleObject': ([w.HANDLE, w.DWORD], w.DWORD),
        'GetCurrentProcess': ([], w.HANDLE),
        'CloseHandle': ([w.HANDLE], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


class _WindowsJob:
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w

        class BasicLimit(c.Structure):
            _fields_ = [('process_time', c.c_longlong), ('job_time', c.c_longlong),
                        ('flags', w.DWORD), ('min_working_set', c.c_size_t),
                        ('max_working_set', c.c_size_t), ('active_process_limit', w.DWORD),
                        ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]

        class ExtendedLimit(c.Structure):
            _fields_ = [('basic', BasicLimit), ('io_counters', c.c_ulonglong * 6),
                        ('process_memory', c.c_size_t), ('job_memory', c.c_size_t),
                        ('peak_process_memory', c.c_size_t), ('peak_job_memory', c.c_size_t)]

        self.api = _windows_api()
        self.name = 'Local\\VideoCollector-' + uuid.uuid4().hex
        self.handle = self.api.CreateJobObjectW(None, self.name)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        limits = ExtendedLimit()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
            error = c.WinError(c.get_last_error())
            self.close()
            raise error

    def close(self):
        if not self.handle:
            return
        import ctypes as c
        from ctypes import wintypes as w
        members = []
        try:
            # Kill-on-close requests termination, but is not an exit barrier.
            # Retain handles to current members *before* they leave job accounting.
            capacity = 32
            while True:
                class ProcessList(c.Structure):
                    _fields_ = [('assigned', w.DWORD), ('count', w.DWORD),
                                ('pids', c.c_size_t * capacity)]
                info = ProcessList()
                ok = self.api.QueryInformationJobObject(self.handle, 3, c.byref(info), c.sizeof(info), None)
                if ok and info.count >= info.assigned:
                    break
                if not ok and c.get_last_error() != 234:  # ERROR_MORE_DATA
                    raise c.WinError(c.get_last_error())
                capacity = max(capacity * 2, info.assigned)
                if capacity > 65536:
                    raise RuntimeError('Task process tree exceeded the tracking limit')
            for pid in info.pids[:info.count]:
                process = self.api.OpenProcess(0x00101000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
                if not process:
                    if c.get_last_error() == 87:  # Process already gone
                        continue
                    raise c.WinError(c.get_last_error())
                member = w.BOOL()
                if not self.api.IsProcessInJob(process, self.handle, c.byref(member)):
                    error = c.WinError(c.get_last_error())
                    self.api.CloseHandle(process)
                    raise error
                if member.value:
                    members.append(process)
                else:
                    self.api.CloseHandle(process)  # PID reused; do not wait on another task.
        finally:
            handle, self.handle = self.handle, None
            try:
                if not self.api.CloseHandle(handle):
                    raise c.WinError(c.get_last_error())
                deadline = time.monotonic() + 10
                for process in members:
                    remaining = max(0, int((deadline - time.monotonic()) * 1000))
                    result = self.api.WaitForSingleObject(process, remaining)
                    if result == 258:
                        raise RuntimeError('任务进程尚未确认退出')
                    if result != 0:
                        raise c.WinError(c.get_last_error())
            finally:
                for process in members:
                    self.api.CloseHandle(process)


def _windows_child(name, args):
    """Join before spawning; failure must never launch an unmanaged command."""
    api = _windows_api()
    handle = api.OpenJobObjectW(0x0001, False, name)  # JOB_OBJECT_ASSIGN_PROCESS
    if not handle:
        return 125
    try:
        joined = api.AssignProcessToJobObject(handle, api.GetCurrentProcess())
    finally:
        api.CloseHandle(handle)
    if not joined:
        return 125
    try:
        return subprocess.call(args, shell=False, stdin=subprocess.DEVNULL,
                               stdout=sys.stdout, stderr=sys.stderr,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError:
        print('Cannot start configured task command', file=sys.stderr)
        return 126


class ProcessCancelled(Exception):
    pass


def run_managed(args, *, timeout, check=False, capture_output=False, stdout_path=None, cancel_check=None):
    """Run an argument list, then clean its process group on every exit path.

    Output goes to temporary files, so a descendant holding an inherited pipe
    cannot prevent timeout cleanup. No shell or broad process-name kill is used.
    """
    if not isinstance(args, (list, tuple)) or not args or not all(isinstance(x, str) for x in args):
        raise ValueError('Task command must be a nonempty argument list')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Task timeout must be a finite positive number')
    if stdout_path is not None and not capture_output:
        raise ValueError('Persistent output requires capture_output')
    args = list(args)
    job = None
    proc = None
    # A caller may retain its own task-bound stdout even after timeout/process
    # death. Exclusive creation prevents accidental replacement of old evidence.
    with (open(stdout_path, 'x+b') if stdout_path is not None else tempfile.TemporaryFile()) as out, tempfile.TemporaryFile() as err:
        try:
            job = _WindowsJob() if os.name == 'nt' else None
            command = ([sys.executable, str(Path(__file__).resolve()), '--windows-job', job.name, '--', *args]
                       if job else args)
            launch = {'creationflags': subprocess.CREATE_NO_WINDOW} if job else {'start_new_session': True}
            proc = subprocess.Popen(command, shell=False, stdin=subprocess.DEVNULL,
                                    stdout=out if capture_output else subprocess.DEVNULL,
                                    stderr=err if capture_output else subprocess.DEVNULL, **launch)
            try:
                if cancel_check is None:
                    proc.wait(timeout=timeout)
                else:
                    deadline = time.monotonic() + timeout
                    while True:
                        if cancel_check():
                            raise ProcessCancelled('任务已请求取消')
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(args, timeout)
                        try:
                            proc.wait(timeout=min(0.2, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            continue
            except subprocess.TimeoutExpired:
                raise subprocess.TimeoutExpired(args, timeout) from None
        finally:
            try:
                if job:
                    job.close()
                elif proc:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            finally:
                if proc:
                    # Also covers a Windows helper still starting before joining.
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=10)
        stdout = stderr = None
        if capture_output:
            out.seek(0)
            err.seek(0)
            stdout = out.read().decode('utf-8', errors='replace')
            stderr = err.read().decode('utf-8', errors='replace')
    result = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result


if __name__ == '__main__':
    if os.name != 'nt' or len(sys.argv) < 5 or sys.argv[1] != '--windows-job' or sys.argv[3] != '--':
        raise SystemExit('Internal process helper; import run_managed instead')
    raise SystemExit(_windows_child(sys.argv[2], sys.argv[4:]))
