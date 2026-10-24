#!/usr/bin/env python3
"""Concurrency tests: many operations in flight on one ring at once."""

import asyncio
import collections
import gc
import os
import sys
import threading
import weakref

import pytest

import urio
from urio.backend import _native_ok


@pytest.mark.skipif(not _native_ok(), reason='io_uring not available')
async def test_cancelled_fixed_close_recycles_only_after_success():
    from urio.driver import Driver

    class FakeRing:
        completions = []

        def submit_close_fixed(self, slot):
            return 42

        def reap(self):
            completed, self.completions = self.completions, []
            return completed

    driver = Driver.__new__(Driver)
    driver._loop = asyncio.get_running_loop()
    driver._ring = FakeRing()
    driver._closed = False
    driver._depth = 8
    driver._inflight = 0
    driver._waiters = collections.deque()
    driver._futures = {}
    driver._unflushed = []
    driver._untracked = set()
    driver._open_ids = set()
    driver._chains = {}
    driver._recycle_on_done = {}
    driver._slots = collections.deque()
    driver._schedule_flush = lambda: None

    task = asyncio.create_task(driver.close_fixed(7))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(driver._slots) == []

    driver._ring.completions = [(42, 0, None)]
    driver._reap()
    assert list(driver._slots) == [7]
    assert driver._inflight == 0


async def test_many_concurrent_writes_and_reads(backend, tmp_path):
    n = 100
    paths = [tmp_path / f'f{i}' for i in range(n)]

    async def write(i, p):
        async with urio.open(p, 'wb') as f:
            await f.write(f'payload-{i}'.encode())

    await asyncio.gather(*(write(i, p) for i, p in enumerate(paths)))

    async def read(p):
        async with urio.open(p, 'rb') as f:
            return await f.read()

    results = await asyncio.gather(*(read(p) for p in paths))

    assert results == [f'payload-{i}'.encode() for i in range(n)]


async def test_concurrent_reads_same_file_distinct_offsets(backend, tmp_path):
    p = tmp_path / 'shared'
    payload = os.urandom(64 * 1024)
    async with urio.open(p, 'wb') as f:
        await f.write(payload)

    async def read_slice(start, length):
        async with urio.open(p, 'rb') as f:
            await f.seek(start)
            return start, await f.read(length)

    jobs = [read_slice(i * 1000, 1500) for i in range(40)]
    results = await asyncio.gather(*jobs)

    for start, data in results:
        assert data == payload[start : start + 1500]


async def test_inflight_returns_to_zero(backend, tmp_path):
    if backend != 'uring':
        return
    from urio.driver import get_driver

    async def op(i):
        async with urio.open(tmp_path / f'g{i}', 'wb') as f:
            await f.write(b'x' * 100)

    await asyncio.gather(*(op(i) for i in range(50)))

    driver = get_driver()
    assert driver.inflight == 0


def test_driver_collected_after_loop_close(backend, tmp_path):
    if backend != 'uring':
        return
    from urio.driver import get_driver

    refs = []

    async def use(i):
        async with urio.open(tmp_path / f'h{i}', 'wb') as f:
            await f.write(b'x')
        refs.append(weakref.ref(get_driver()))

    for i in range(3):
        asyncio.run(use(i))
    gc.collect()

    assert all(r() is None for r in refs)


def test_loop_gc_on_another_thread_is_silent(backend, tmp_path, monkeypatch):
    if backend != 'uring':
        pytest.skip('cross-thread GC of the native ring is uring-specific')
    from urio.driver import get_driver

    holder = []
    driver_ref = []

    async def use():
        holder.append(asyncio.get_running_loop())
        async with urio.open(tmp_path / 'x', 'wb') as f:
            await f.write(b'x')
        driver_ref.append(weakref.ref(get_driver()))

    t = threading.Thread(target=lambda: asyncio.run(use()))
    t.start()
    t.join()

    gc.collect()
    unraisable = []
    monkeypatch.setattr(sys, 'unraisablehook', lambda args: unraisable.append(args))
    holder.clear()
    gc.collect()

    assert driver_ref[0]() is None
    assert unraisable == []


@pytest.mark.parametrize(
    ('raw', 'expected'),
    (
        pytest.param('2048', 2048, id='override'),
        pytest.param('999999', 32768, id='clamped-to-kernel-cap'),
        pytest.param('0', 512, id='zero-falls-back-to-default'),
        pytest.param('nope', 512, id='garbage-falls-back-to-default'),
    ),
)
def test_ring_entries_env_knob(monkeypatch, raw, expected):
    from urio import driver

    monkeypatch.setenv('URIO_RING_ENTRIES', raw)

    assert driver._ring_entries() == expected


async def test_aclose_releases_driver_and_allows_reuse(backend, tmpfile):
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'before')

    await urio.aclose()

    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == b'before'


async def test_gather_does_not_interleave_corrupt(backend, tmp_path):
    async def write(i):
        p = tmp_path / f'big{i}'
        data = bytes([i % 256]) * (200 * 1024)
        async with urio.open(p, 'wb') as f:
            await f.write(data)
        async with urio.open(p, 'rb') as f:
            return await f.read() == data

    oks = await asyncio.gather(*(write(i) for i in range(20)))

    assert all(oks)
