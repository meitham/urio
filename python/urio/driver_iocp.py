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
import errno
import os
import weakref

from . import _uring

_DRIVER_ATTR = '_urio_iocp_driver'
_drivers: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _error_message(code: int) -> str:
    return (
        ctypes.FormatError(code)
        if hasattr(ctypes, 'FormatError')
        else os.strerror(code)
    )


class IocpDriver:
    """Bridges one IOCP into a single asyncio event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._futures: dict[int, asyncio.Future] = {}
        self._open_ids: set[int] = set()
        self._failed: tuple[int, str] | None = None
        self._closed = False
        self._iocp = _uring.Iocp(loop, self._on_complete, self._on_fatal_error)

    def _report_error(self, message: str, error: Exception) -> None:
        self._loop.call_exception_handler({'message': message, 'exception': error})

    def _error(self) -> OSError:
        if self._failed is not None:
            code, message = self._failed
            return OSError(0, message, None, code)
        return OSError(errno.EBADF, 'IOCP driver is closed')

    def _on_fatal_error(self, code: int) -> None:
        if self._failed is not None:
            return
        self._failed = (code, _error_message(code))
        for fut in self._futures.values():
            if not fut.done():
                fut.set_exception(self._error())
        self._futures.clear()
        self._report_error('IOCP poller failed', self._error())

    def _on_complete(self, batch) -> None:
        for item in batch:
            try:
                self._complete_one(item)
            except Exception as error:
                self._on_fatal_error(getattr(error, 'winerror', None) or 8)

    def _complete_one(self, item) -> None:
        ident, status, value, data = item
        fut = self._futures.get(ident)
        was_open = ident in self._open_ids
        if fut is None or fut.done():
            if was_open and status == 0:
                try:
                    self._iocp.close_now(value)
                except OSError as error:
                    self._report_error('Could not close an abandoned file', error)
            elif status != 0:
                self._report_error(
                    'Cancelled IOCP operation failed',
                    OSError(0, _error_message(status), None, status),
                )
        elif status != 0:
            fut.set_exception(OSError(0, _error_message(status), None, status))
        else:
            fut.set_result((value, data))
        self._futures.pop(ident, None)
        self._open_ids.discard(ident)

    async def _run(self, submit, *, opens_handle: bool = False):
        if self._failed is not None or self._closed:
            raise self._error()
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
        except OSError as error:
            self._report_error('Cancelled file open failed', error)
            return
        except BaseException:
            return
        try:
            self._iocp.close_now(handle)
        except OSError as error:
            self._report_error('Could not close an abandoned file', error)

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
        if self._closed or self._failed is not None:
            self._iocp.close_now(handle)
            return
        await self._run(lambda i: i.submit_close(handle))

    def shutdown(self) -> None:
        """Stop the poller thread and fail any still-pending futures."""
        if self._closed:
            return
        self._closed = True
        try:
            self._iocp.shutdown()
        finally:
            for fut in self._futures.values():
                if not fut.done():
                    fut.set_exception(self._error())
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
