import asyncio
import collections
import errno
import sys

import pytest

from urio.backend import IocpBackend
from urio.driver import Driver
from urio.driver_iocp import IocpDriver
from urio.driver_iocp_proactor import ProactorDriver
from urio.file import AsyncBufferedFile


async def test_proactor_registration_failure_discards_unstarted_operation():
    class Io:
        def __init__(self):
            self.discarded = []

        def discard(self, address):
            self.discarded.append(address)

    class Proactor:
        def _register(self, ov, handle, callback):
            raise RuntimeError('proactor is closed')

    driver = object.__new__(ProactorDriver)
    driver._io = Io()
    driver._proactor = Proactor()
    started = []
    with pytest.raises(RuntimeError, match='closed'):
        await driver._await_op(123, 456, lambda *args: started.append(args))
    assert driver._io.discarded == [123]
    assert started == []


async def test_iocp_poller_failure_fails_pending_and_future_operations():
    loop = asyncio.get_running_loop()
    contexts = []
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: contexts.append(context))
    try:
        driver = object.__new__(IocpDriver)
        driver._loop = loop
        future = loop.create_future()
        driver._futures = {1: future}
        driver._open_ids = set()
        driver._failed = None
        driver._closed = False
        driver._on_fatal_error(6)
        with pytest.raises(OSError) as error:
            await asyncio.wait_for(future, 1)
        if sys.platform == 'win32':
            assert error.value.winerror == 6
        with pytest.raises(OSError) as error:
            await driver._run(lambda io: 2)
        if sys.platform == 'win32':
            assert error.value.winerror == 6
        assert contexts[0]['message'] == 'IOCP poller failed'
    finally:
        loop.set_exception_handler(old_handler)


async def test_iocp_shutdown_failure_still_fails_waiters():
    class Io:
        def shutdown(self):
            raise OSError(errno.EIO, 'shutdown failed')

    loop = asyncio.get_running_loop()
    driver = object.__new__(IocpDriver)
    driver._loop = loop
    driver._iocp = Io()
    future = loop.create_future()
    driver._futures = {1: future}
    driver._open_ids = set()
    driver._failed = None
    driver._closed = False
    with pytest.raises(OSError, match='shutdown failed'):
        driver.shutdown()
    with pytest.raises(OSError) as error:
        await future
    assert error.value.errno == errno.EBADF


async def test_linux_submit_error_keeps_slot_until_completion():
    class Ring:
        completions = []

        def submit(self):
            raise OSError(errno.EIO, 'submit failed')

        def reap(self):
            result, self.completions = self.completions, []
            return result

    driver = object.__new__(Driver)
    driver._loop = asyncio.get_running_loop()
    driver._ring = Ring()
    driver._closed = False
    driver._failed = None
    driver._depth = 8
    driver._inflight = 1
    driver._waiters = collections.deque()
    future = driver._loop.create_future()
    driver._futures = {7: future}
    driver._unflushed = [7]
    driver._flush_scheduled = True
    driver._flush_retries = 0
    driver._untracked = set()
    driver._open_ids = set()
    driver._chains = {}
    driver._recycle_on_done = {}
    driver._slots = collections.deque()
    driver._flush()
    with pytest.raises(OSError, match='submit failed'):
        await future
    assert driver._inflight == 1
    assert 7 in driver._futures
    driver._ring.completions = [(7, -errno.EIO, None)]
    driver._reap()
    assert driver._inflight == 0


async def test_file_close_continues_after_awaiter_cancellation():
    entered = asyncio.Event()
    release = asyncio.Event()

    class Backend:
        async def close(self, fd):
            entered.set()
            await release.wait()

    file = object.__new__(AsyncBufferedFile)
    file._closed = False
    file._close_task = None
    file._backend = Backend()
    file._fd = 7
    first = asyncio.create_task(file.close())
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(file.close())
    release.set()
    await asyncio.wait_for(second, 1)
    assert file.closed


async def test_file_close_error_after_cancellation_reaches_loop_handler():
    entered = asyncio.Event()
    release = asyncio.Event()
    reported = asyncio.Event()
    loop = asyncio.get_running_loop()
    contexts = []
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda loop, context: (contexts.append(context), reported.set())
    )

    class Backend:
        async def close(self, fd):
            entered.set()
            await release.wait()
            raise OSError(errno.EIO, 'close failed')

    try:
        file = object.__new__(AsyncBufferedFile)
        file._closed = False
        file._close_task = None
        file._backend = Backend()
        file._fd = 7
        task = asyncio.create_task(file.close())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.wait_for(reported.wait(), 1)
        assert contexts[0]['message'] == 'File close failed after cancellation'
        assert isinstance(contexts[0]['exception'], OSError)
    finally:
        loop.set_exception_handler(old_handler)


async def test_iocp_handle_keeps_its_original_driver_after_shutdown(monkeypatch):
    class Driver:
        closed = False

        async def open(self, *args):
            return 12345

        async def read(self, *args):
            if self.closed:
                raise OSError(errno.EBADF, 'IOCP driver is closed')
            return b'data'

        async def close(self, *args):
            pass

    old = Driver()
    new = Driver()
    backend = IocpBackend()
    monkeypatch.setattr(backend, '_driver', lambda: old)
    handle = await backend.open('path', 0, 0)
    old.closed = True
    monkeypatch.setattr(backend, '_driver', lambda: new)
    try:
        with pytest.raises(OSError, match='closed'):
            await backend.read(handle, 0, 1)
    finally:
        await backend.close(handle)
