"""One scheduler per monitor database, independent of HTTP bind address."""
from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from contextlib import contextmanager
from functools import wraps
from pathlib import Path


@contextmanager
def _exclusive(path: Path, message: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+', encoding='utf-8') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(message) from None
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    # Keep the inode: unlinking a lock file lets waiting processes lock different files.


def monitor_instance(database: Path):
    # Resolve aliases so a symlink cannot create a second owner of the same database.
    database = database.expanduser().resolve()
    return _exclusive(database.with_suffix(database.suffix + '.service.lock'),
                      '该数据库已有监控服务运行，请使用现有页面；不能重复启动采集后台')


def device_session(udid: str):
    # Shared across worktrees/databases; no device identifiers stored on disk.
    name = hashlib.sha256(udid.encode()).hexdigest() + '.lock'
    path = Path(tempfile.gettempdir()) / f'goofish-device-locks-{os.getuid()}' / name
    return _exclusive(path, '这台iPhone正在被另一个采集任务使用，本次未创建新会话，请等待该任务结束')


def exclusive_device(method):
    @wraps(method)
    def collect(self, udid, *args, **kwargs):
        # Lease belongs to the worker, so cancellation of its async caller does
        # not release it while the iPhone thread is still executing commands.
        with device_session(udid):
            return method(self, udid, *args, **kwargs)
    return collect
