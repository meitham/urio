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

The IOCP backend is opt-in. Earlier Windows 10/11 measurements with CPython
3.14 found slower concurrent binary reads than the thread backend; see the
benchmark documentation for the workload and limits of those measurements.
Windows uses the thread backend by default.
"""

from __future__ import annotations

import asyncio
import ctypes
import weakref

from . import _uring

_DRIVER_ATTR = '_urio_iocp_driver'
_drivers: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class IocpDriver:
    """Bridges one IOCP into a single asyncio event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._futures: dict[int, asyncio.Future] = {}
        self._open_ids: set[int] = set()
        self._iocp = _uring.Iocp(loop, self._on_complete)

    def _on_complete(self, batch) -> None:
        for ident, status, value, data in batch:
            fut = self._futures.pop(ident, None)
            was_open = ident in self._open_ids
            self._open_ids.discard(ident)
            if fut is None or fut.done():
                if was_open and status == 0:
                    try:
                        self._iocp.close_now(value)
                    except OSError:
                        pass
                continue
            if status != 0:
                fut.set_exception(OSError(0, ctypes.FormatError(status), None, status))
            else:
                fut.set_result((value, data))

    async def _run(self, submit, *, opens_handle: bool = False):
        fut = self._loop.create_future()
        ident = submit(self._iocp)
        self._futures[ident] = fut
        if opens_handle:
            self._open_ids.add(ident)
        try:
            return await asyncio.shield(fut) if opens_handle else await fut
        except asyncio.CancelledError:
            if opens_handle:
                if fut.done():
                    self._close_abandoned_open(fut)
                else:
                    fut.add_done_callback(self._close_abandoned_open)
            raise

    def _close_abandoned_open(self, fut: asyncio.Future) -> None:
        try:
            handle, _ = fut.result()
        except BaseException:
            return
        try:
            self._iocp.close_now(handle)
        except OSError:
            pass

    async def open(
        self, path: str, write: bool, create: bool, truncate: bool, exclusive: bool
    ) -> int:
        handle, _ = await self._run(
            lambda i: i.submit_open(path, write, create, truncate, exclusive),
            opens_handle=True,
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
