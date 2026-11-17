"""Crash-safe, non-blocking process locks. A persistent file is not itself a lock."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time

from common import CheckError


class RunBusy(CheckError):
    pass


_held=ContextVar('hunter_held_locks',default=frozenset())


def _alive(pid):
    if pid<=0:return False
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x100000,False,pid)
        if not handle:return ctypes.get_last_error()==5  # Access denied: do not steal.
        try:return kernel.WaitForSingleObject(handle,0)==258
        finally:kernel.CloseHandle(handle)
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False
    except PermissionError:return True


def _lock(stream):
    stream.seek(0)
    if os.name=='nt':
        import msvcrt
        msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)


def _unlock(stream):
    stream.seek(0)
    if os.name=='nt':
        import msvcrt
        msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


@contextmanager
def one_run(path):
    path=Path(path).resolve();name=str(path).casefold()
    if name in _held.get():
        yield
        return
    path.parent.mkdir(parents=True,exist_ok=True)
    stream=os.fdopen(os.open(path,os.O_RDWR|os.O_CREAT,0o600),'r+b',buffering=0)
    locked=False;token=None
    try:
        if os.fstat(stream.fileno()).st_size==0:stream.write(b' ')
        for attempt in range(3):
            # A status probe (web UI every 2.5 s, the bot) holds the lock for milliseconds: wait that out (audit A21).
            try:_lock(stream);locked=True;break
            except OSError:
                if attempt==2:raise RunBusy('Программа сейчас занята другой операцией (подготовка писем, отклик HH или сохранение).') from None
                time.sleep(0.1)
        stream.seek(0);previous=stream.read().strip()
        # Respect a still-running pre-upgrade version which used PID-only files.
        if previous.isdigit() and _alive(int(previous)):
            raise RunBusy('Предыдущая версия программы ещё работает. Дождитесь её завершения.')
        stream.seek(0);stream.write(json.dumps({'version':2,'pid':os.getpid()}).encode());stream.truncate()
        token=_held.set(_held.get()|{name})
        yield
    finally:
        if token is not None:_held.reset(token)
        if locked:_unlock(stream)
        stream.close()
    # Never unlink: deleting a released file races with the next owner's handle.


def is_busy(path):
    try:
        with one_run(path):return False
    except RunBusy:return True
