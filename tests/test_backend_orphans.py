#!/usr/bin/env python3
"""Fd safety around cancelled executor jobs: a job cancelled while running
keeps using its fd, so close() must wait for it before the fd number can be
recycled — on every backend, since the native ones inherit executor-backed
fallbacks (ftruncate, writev, filesize) that park orphans the same way."""

import asyncio
import os
import threading

import pytest

import urio
import urio.backend as backend_mod
from urio.backend import ThreadBackend


@pytest.fixture(name='slow_pread')
def fixture_slow_pread(monkeypatch):
    """Given: a _pread that blocks until released, signalling when running."""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    orig = backend_mod._pread

    def slow(fd, n, offset):
        started.set()
        release.wait(5)
        try:
            return orig(fd, n, offset)
        finally:
            finished.set()

    monkeypatch.setattr(backend_mod, '_pread', slow)
    return started, release, finished


async def test_close_waits_for_cancelled_running_read(tmpfile, slow_pread):
    started, release, finished = slow_pread
    tmpfile.write_bytes(b'x' * 64)
    b = ThreadBackend()
    fd = await b.open(str(tmpfile), os.O_RDONLY, 0o644)

    task = asyncio.ensure_future(b.read(fd, 0, 16))
    while not started.is_set():
        await asyncio.sleep(0.005)

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    closer = asyncio.ensure_future(b.close(fd))
    await asyncio.sleep(0.05)

    assert not closer.done()

    release.set()
    await closer

    assert finished.is_set()
    assert fd not in backend_mod._orphans


async def test_close_drains_orphans_on_every_backend(backend, tmpfile):
    tmpfile.write_bytes(b'x')
    b = urio.get_backend()
    fd = await b.open(str(tmpfile), os.O_RDONLY, 0o644)
    release = threading.Event()
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(None, release.wait, 5)
    backend_mod._orphans.setdefault(fd, set()).add(fut)
    fut.add_done_callback(lambda f, fd=fd: backend_mod._reap_orphan(fd, f))

    closer = asyncio.ensure_future(b.close(fd))
    await asyncio.sleep(0.05)

    assert not closer.done()

    release.set()
    await closer

    assert fd not in backend_mod._orphans


async def test_cancelled_thread_open_closes_late_descriptor(tmpfile, monkeypatch):
    tmpfile.write_bytes(b'x')
    started = threading.Event()
    release = threading.Event()
    opened = threading.Event()
    descriptors = []
    original_open = os.open

    def slow_open(path, flags, mode):
        started.set()
        release.wait(5)
        fd = original_open(path, flags, mode)
        descriptors.append(fd)
        opened.set()
        return fd

    monkeypatch.setattr(backend_mod.os, 'open', slow_open)
    task = asyncio.create_task(ThreadBackend().open(str(tmpfile), os.O_RDONLY, 0o644))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(opened.wait, 5)
        for _ in range(100):
            try:
                os.fstat(descriptors[0])
            except OSError:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail('cancelled open left its descriptor open')
    finally:
        release.set()
        if descriptors:
            try:
                os.close(descriptors[0])
            except OSError:
                pass
