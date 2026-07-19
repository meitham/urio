"""Windows IOCP integration — poller-thread driver (opt-in via
``URIO_WINDOWS_IOCP=1``).

This is the **fallback** driver, used when the running loop is not a
``ProactorEventLoop`` (e.g. ``SelectorEventLoop``). On a ``ProactorEventLoop``
urio instead uses :mod:`urio.driver_iocp_proactor`, which reaps completions on
the loop thread with no extra thread — see that module.

Here the native ``Iocp`` object opens files with overlapped I/O, associates them
with its own I/O completion port, and runs **one** poller thread that reaps
completions and resolves the matching asyncio futures — true async file I/O with
a single thread for any number of in-flight operations, unlike a thread-per-op
pool. Completions are reaped in batches (``GetQueuedCompletionStatusEx``) and
delivered to the loop with a single ``loop.call_soon_threadsafe`` per batch, so
the cross-thread wakeup is amortised across a burst of ops rather than paid once
per operation.

Validated on Windows 10/11 (CPython 3.14, GIL and free-threaded): the full
file+path test suite and a concurrency/cancellation stress test pass. Opt-in
because warm-cache reads remain memcpy-bound — the read copy is single-threaded,
while aiofiles' thread pool spreads it across cores — so Windows defaults to the
thread-pool backend (aiofiles-class). Writes are zero-copy and at parity;
durable writes win.
"""

from __future__ import annotations

import asyncio
import ctypes
import weakref

from . import _uring

# Cached as an attribute on the loop object so the driver (and its poller
# thread + completion port) dies with the loop. A WeakKeyDictionary keyed by
# loop would never release entries: the driver strongly references the loop, so
# the dict's strong value reference keeps the key alive forever. The dict below
# is only a weak-VALUE fallback for loops that reject attribute assignment.
_DRIVER_ATTR = '_urio_iocp_driver'
_drivers: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class IocpDriver:
    """Bridges one IOCP into a single asyncio event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._futures: dict[int, asyncio.Future] = {}
        # The native poller thread delivers a whole batch of completions via one
        # call_soon_threadsafe(self._on_complete, batch), so it runs back on the
        # loop thread.
        self._iocp = _uring.Iocp(loop, self._on_complete)

    def _on_complete(self, batch) -> None:
        # batch is a list of (ident, status, value, data) tuples reaped together.
        for ident, status, value, data in batch:
            fut = self._futures.pop(ident, None)
            if fut is None or fut.done():
                continue
            if status != 0:
                # status is a Win32 error code; FormatError gives a real message
                # and the code is preserved as the OSError winerror.
                fut.set_exception(OSError(0, ctypes.FormatError(status), None, status))
            else:
                fut.set_result((value, data))

    async def _run(self, submit):
        fut = self._loop.create_future()
        ident = submit(self._iocp)
        self._futures[ident] = fut
        return await fut

    async def open(
        self, path: str, write: bool, create: bool, truncate: bool, exclusive: bool
    ) -> int:
        handle, _ = await self._run(
            lambda i: i.submit_open(path, write, create, truncate, exclusive)
        )
        return handle

    async def read(self, handle: int, offset: int, length: int) -> bytes:
        _, data = await self._run(lambda i: i.submit_read(handle, offset, length))
        return data if data is not None else b''

    async def write(self, handle: int, offset: int, data: bytes) -> int:
        nbytes, _ = await self._run(lambda i: i.submit_write(handle, offset, data))
        return nbytes

    async def close(self, handle: int) -> None:
        await self._run(lambda i: i.submit_close(handle))

    def shutdown(self) -> None:
        """Stop the poller thread and fail any still-pending futures."""
        self._iocp.shutdown()
        for fut in self._futures.values():
            if not fut.done():
                fut.cancel()
        self._futures.clear()


def get_iocp_driver(loop: asyncio.AbstractEventLoop | None = None) -> IocpDriver:
    if loop is None:
        loop = asyncio.get_running_loop()
    driver = getattr(loop, _DRIVER_ATTR, None)
    if driver is None:
        ref = _drivers.get(loop)
        driver = ref() if ref is not None else None
    if driver is None:
        driver = IocpDriver(loop)
        try:
            setattr(loop, _DRIVER_ATTR, driver)
        except AttributeError:
            _drivers[loop] = weakref.ref(driver)
    return driver


def close_driver(loop: asyncio.AbstractEventLoop) -> None:
    """Shut down and discard the driver cached on ``loop``, if any."""
    driver = getattr(loop, _DRIVER_ATTR, None)
    if driver is not None:
        try:
            delattr(loop, _DRIVER_ATTR)
        except AttributeError:
            pass
    else:
        ref = _drivers.pop(loop, None)
        driver = ref() if ref is not None else None
    if driver is not None:
        driver.shutdown()
