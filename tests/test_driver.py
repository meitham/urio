#!/usr/bin/env python3
"""Concurrency tests: many operations in flight on one ring at once."""

import asyncio
import gc
import os
import weakref

import urio


async def test_many_concurrent_writes_and_reads(backend, tmp_path):
    # given: a hundred distinct files to write concurrently
    n = 100
    paths = [tmp_path / f'f{i}' for i in range(n)]

    async def write(i, p):
        async with urio.open(p, 'wb') as f:
            await f.write(f'payload-{i}'.encode())

    # when: writing them all concurrently then reading concurrently
    await asyncio.gather(*(write(i, p) for i, p in enumerate(paths)))

    async def read(p):
        async with urio.open(p, 'rb') as f:
            return await f.read()

    results = await asyncio.gather(*(read(p) for p in paths))

    # then: every file holds exactly its own payload
    assert results == [f'payload-{i}'.encode() for i in range(n)]


async def test_concurrent_reads_same_file_distinct_offsets(backend, tmp_path):
    # given: one file with known content
    p = tmp_path / 'shared'
    payload = os.urandom(64 * 1024)
    async with urio.open(p, 'wb') as f:
        await f.write(payload)

    async def read_slice(start, length):
        async with urio.open(p, 'rb') as f:
            await f.seek(start)
            return start, await f.read(length)

    # when: reading many overlapping slices concurrently
    jobs = [read_slice(i * 1000, 1500) for i in range(40)]
    results = await asyncio.gather(*jobs)

    # then: each slice matches the corresponding region of the original
    for start, data in results:
        assert data == payload[start : start + 1500]


async def test_inflight_returns_to_zero(backend, tmp_path):
    # given: the uring backend exposes its driver
    if backend != 'uring':
        return
    from urio.driver import get_driver

    # when: running a batch of concurrent operations
    async def op(i):
        async with urio.open(tmp_path / f'g{i}', 'wb') as f:
            await f.write(b'x' * 100)

    await asyncio.gather(*(op(i) for i in range(50)))

    # then: no submissions remain pending afterwards
    driver = get_driver()
    assert driver.inflight == 0


def test_driver_collected_after_loop_close(backend, tmp_path):
    # given: an event loop that performed I/O (and so lazily created a driver)
    if backend != 'uring':
        return
    from urio.driver import get_driver

    refs = []

    async def use(i):
        async with urio.open(tmp_path / f'h{i}', 'wb') as f:
            await f.write(b'x')
        refs.append(weakref.ref(get_driver()))

    # when: running (and thereby closing) several short-lived loops
    for i in range(3):
        asyncio.run(use(i))
    gc.collect()

    # then: every driver (and its ring/eventfd) has been reclaimed
    assert all(r() is None for r in refs)


async def test_aclose_releases_driver_and_allows_reuse(backend, tmpfile):
    # given: a driver created by a first operation
    async with urio.open(tmpfile, 'wb') as f:
        await f.write(b'before')

    # when: closing urio's per-loop resources explicitly
    await urio.aclose()

    # then: I/O still works afterwards on a freshly created driver
    async with urio.open(tmpfile, 'rb') as f:
        assert await f.read() == b'before'


async def test_gather_does_not_interleave_corrupt(backend, tmp_path):
    # given: large concurrent writes that span multiple chunks
    async def write(i):
        p = tmp_path / f'big{i}'
        data = bytes([i % 256]) * (200 * 1024)
        async with urio.open(p, 'wb') as f:
            await f.write(data)
        async with urio.open(p, 'rb') as f:
            return await f.read() == data

    # when: running 20 such writers concurrently
    oks = await asyncio.gather(*(write(i) for i in range(20)))

    # then: none of them corrupted each other's buffers
    assert all(oks)
