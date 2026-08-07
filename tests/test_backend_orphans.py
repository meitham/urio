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

    # given: a read whose executor job is already running
    task = asyncio.ensure_future(b.read(fd, 0, 16))
    while not started.is_set():
        await asyncio.sleep(0.005)

    # when: the awaiting task is cancelled mid-job and the fd is closed
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    closer = asyncio.ensure_future(b.close(fd))
    await asyncio.sleep(0.05)

    # then: close does not complete while the job still uses the fd
    assert not closer.done()

    # when: the job finishes
    release.set()
    await closer

    # then: the fd was released only after the job ended, and no orphan remains
    assert finished.is_set()
    assert fd not in backend_mod._orphans


async def test_close_drains_orphans_on_every_backend(backend, tmpfile):
    # given: an fd opened through the active backend with a parked
    #        cancelled-but-running executor job (as a cancelled fallback op
    #        like ftruncate or writev would leave behind)
    tmpfile.write_bytes(b'x')
    b = urio.get_backend()
    fd = await b.open(str(tmpfile), os.O_RDONLY, 0o644)
    release = threading.Event()
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(None, release.wait, 5)
    backend_mod._orphans.setdefault(fd, set()).add(fut)
    fut.add_done_callback(lambda f, fd=fd: backend_mod._reap_orphan(fd, f))

    # when: closing the fd while the job is still running
    closer = asyncio.ensure_future(b.close(fd))
    await asyncio.sleep(0.05)

    # then: close waits for the job rather than freeing the fd under it
    assert not closer.done()

    # when: the job finishes
    release.set()
    await closer

    # then: the close completed and the orphan registry is clean
    assert fd not in backend_mod._orphans
