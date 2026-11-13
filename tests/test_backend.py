#!/usr/bin/env python3
import asyncio
import os
import threading
import time

import pytest

import urio
from urio.backend import (
    IocpBackend,
    ThreadBackend,
    UringBackend,
    _native_ok,
    get_backend,
)


def test_set_backend_invalid_name():

    with pytest.raises(ValueError):
        urio.set_backend('nonsense')

    urio.set_backend('auto')


def test_force_thread_backend():

    urio.set_backend('thread')
    try:
        backend = get_backend()
        assert isinstance(backend, ThreadBackend)
        assert not isinstance(backend, UringBackend)
        assert backend.name == 'thread'
    finally:
        urio.set_backend('auto')


@pytest.mark.skipif(not _native_ok(), reason='io_uring not available')
def test_force_uring_backend():
    urio.set_backend('uring')
    try:
        backend = get_backend()
        assert isinstance(backend, UringBackend)
        assert backend.name == 'uring'
    finally:
        urio.set_backend('auto')


def test_env_var_selects_thread(monkeypatch):
    monkeypatch.setenv('URIO_BACKEND', 'thread')
    urio.set_backend('auto')
    import urio.backend as b

    b._backend = None

    assert get_backend().name == 'thread'
    b._backend = None


async def test_stat_result_fields_match_os(backend, tmp_path):
    p = tmp_path / 'cmp'
    p.write_bytes(b'abcdef')

    st = await get_backend().statx(str(p))
    ref = os.stat(str(p))

    assert st.st_size == ref.st_size == 6
    assert st.st_mode == ref.st_mode
    assert st.st_ino == ref.st_ino
    assert st.st_mtime_ns == ref.st_mtime_ns


async def test_iocp_handle_errors_are_reported(backend):
    if backend != 'iocp':
        pytest.skip('Windows IOCP only')
    active = get_backend()
    with pytest.raises(OSError):
        await active.fsync(-1)
    with pytest.raises(OSError):
        await active.filesize(-1)
    with pytest.raises(OSError):
        await active.ftruncate(-1, 0)


async def test_close_invalid_handle_raises(backend):
    invalid = 0 if backend == 'iocp' else -1
    with pytest.raises(OSError):
        await get_backend().close(invalid)


async def test_iocp_readall_sizes_small_reads(monkeypatch):
    active = IocpBackend()
    requests = []

    class Driver:
        async def read(self, fd, offset, length):
            requests.append((offset, length))
            return b'x'[offset : offset + length]

    async def filesize(fd):
        return 1

    monkeypatch.setattr(active, '_driver', Driver)
    monkeypatch.setattr(active, 'filesize', filesize)
    assert await active.readall(1, 0) == b'x'
    assert requests == [(0, 1), (1, 1)]


async def test_thread_readall_serializes_shared_fd(tmp_path, monkeypatch):
    path = tmp_path / 'shared'
    path.write_bytes(b'abcdef')
    active = ThreadBackend()
    fd = await active.open(str(path), os.O_RDONLY, 0)
    original_lseek = os.lseek
    entered = threading.Event()

    def slow_lseek(target, offset, whence):
        result = original_lseek(target, offset, whence)
        if target == fd and offset == 0:
            entered.set()
            time.sleep(0.1)
        return result

    monkeypatch.setattr(os, 'lseek', slow_lseek)
    try:
        first = asyncio.create_task(active.readall(fd, 0))
        assert await asyncio.to_thread(entered.wait, 5)
        second = asyncio.create_task(active.readall(fd, 1))
        assert await asyncio.gather(first, second) == [b'abcdef', b'bcdef']
    finally:
        await active.close(fd)


def test_proactor_completion_error_does_not_discard_reused_address():
    from urio.driver_iocp_proactor import ProactorDriver, _Ov

    class FakeIo:
        def __init__(self):
            self.discarded = []

        def take(self, address, transferred):
            raise OSError(5, 'read failed')

        def discard(self, address):
            self.discarded.append(address)

    io = FakeIo()
    driver = object.__new__(ProactorDriver)
    driver._io = io
    ov = _Ov(123, io)
    with pytest.raises(OSError, match='read failed'):
        driver._complete(0, 0, ov)
    ov.__del__()
    assert io.discarded == []
