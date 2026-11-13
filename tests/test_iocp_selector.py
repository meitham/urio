#!/usr/bin/env python3
"""The IOCP poller-thread fallback driver, exercised on a SelectorEventLoop.

The rest of the suite runs on Windows' default ProactorEventLoop and therefore
covers only the loop-thread driver (``urio.driver_iocp_proactor``). These tests
run their coroutines on a ``SelectorEventLoop``, which has no ``_proactor``, so
the backend must fall back to the poller-thread driver (``urio.driver_iocp``).
"""

import asyncio
import sys

import pytest

import urio


def test_selector_loop_engages_poller_driver(selector_loop, tmpfile):
    assert not hasattr(selector_loop, '_proactor')

    async def round_trip():
        async with urio.open(tmpfile, 'w') as f:
            await f.write('hello\nselector\n')
        async with urio.open(tmpfile) as f:
            return await f.read()

    text = selector_loop.run_until_complete(round_trip())

    assert text == 'hello\nselector\n'
    from urio.driver_iocp import IocpDriver

    assert isinstance(selector_loop._urio_iocp_driver, IocpDriver)
    assert not hasattr(selector_loop, '_urio_proactor_driver')


def test_selector_loop_binary_positioned_io(selector_loop, tmpfile):
    async def scenario():
        async with urio.open(tmpfile, 'wb') as f:
            await f.write(b'\x00\x01\x02\x03abcdef')

        async with urio.open(tmpfile, 'rb') as f:
            await f.seek(4)
            head = await f.read(3)
            rest = await f.read()
            return head, rest

    head, rest = selector_loop.run_until_complete(scenario())

    assert head == b'abc'
    assert rest == b'def'


def test_selector_loop_concurrent_fan_out(selector_loop, tmp_path):
    async def save(i):
        async with urio.open(tmp_path / f'{i}.txt', 'w') as f:
            await f.write(f'record {i}\n')

    async def load(i):
        async with urio.open(tmp_path / f'{i}.txt') as f:
            return await f.read()

    async def scenario():
        await asyncio.gather(*(save(i) for i in range(50)))
        return await asyncio.gather(*(load(i) for i in range(50)))

    contents = selector_loop.run_until_complete(scenario())

    assert contents == [f'record {i}\n' for i in range(50)]


def test_selector_loop_error_paths(selector_loop, tmpfile):
    async def read_missing():
        async with urio.open(tmpfile.parent / 'nope', 'rb') as f:
            await f.read()

    async def create_exclusive():
        async with urio.open(tmpfile, 'x') as f:
            await f.write('again')

    async def scenario():
        async with urio.open(tmpfile, 'w') as f:
            await f.write('taken')
        try:
            await read_missing()
        except FileNotFoundError:
            pass
        else:
            raise AssertionError('expected FileNotFoundError')
        try:
            await create_exclusive()
        except FileExistsError:
            pass
        else:
            raise AssertionError('expected FileExistsError')

    selector_loop.run_until_complete(scenario())


def test_selector_loop_cancellation_leaves_driver_healthy(selector_loop, tmp_path):
    p = tmp_path / 'big'

    async def scenario():
        async with urio.open(p, 'wb') as f:
            await f.write(b'x' * (4 * 1024 * 1024))

        async def read_it():
            async with urio.open(p, 'rb') as f:
                return await f.read()

        tasks = [asyncio.ensure_future(read_it()) for _ in range(20)]
        await asyncio.sleep(0)

        for t in tasks:
            t.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(r, (asyncio.CancelledError, bytes)) for r in results)

        async with urio.open(p, 'rb') as f:
            return len(await f.read())

    assert selector_loop.run_until_complete(scenario()) == 4 * 1024 * 1024


def test_selector_loop_aclose_allows_reuse(selector_loop, tmpfile):
    async def write(text):
        async with urio.open(tmpfile, 'a') as f:
            await f.write(text)

    async def scenario():
        await write('one\n')
        await urio.aclose()
        assert not hasattr(selector_loop, '_urio_iocp_driver')
        await write('two\n')
        async with urio.open(tmpfile) as f:
            return await f.read()

    assert selector_loop.run_until_complete(scenario()) == 'one\ntwo\n'


def test_selector_poller_failure_fails_waiters(selector_loop):
    from urio.driver_iocp import get_iocp_driver

    async def scenario():
        loop = asyncio.get_running_loop()
        contexts = []
        loop.set_exception_handler(lambda loop, context: contexts.append(context))
        driver = get_iocp_driver()
        future = loop.create_future()
        driver._futures[999] = future
        driver._iocp._test_fail_poller(6)
        with pytest.raises(OSError) as error:
            await asyncio.wait_for(future, 5)
        assert error.value.winerror == 6
        with pytest.raises(OSError):
            await driver.read(0, 0, 1)
        assert contexts[0]['message'] == 'IOCP poller failed'

    selector_loop.run_until_complete(scenario())


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows IOCP only')
def test_proactor_synthetic_completion_post_failure():
    from urio import _uring

    io = _uring.ProactorIo(0)
    read = io.prepare_read(0, 1)
    with pytest.raises(OSError) as error:
        io.start_read(read, 0, 1)
    assert error.value.winerror == 6

    write = io.prepare_write(0, b'x')
    with pytest.raises(OSError) as error:
        io.start_write(write, 0)
    assert error.value.winerror == 6

    with pytest.raises(OSError, match='unknown IOCP completion'):
        io.take(read, 0)
