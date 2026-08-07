"""ProactorEventLoop-native Windows IOCP driver (opt-in via ``URIO_WINDOWS_IOCP=1``).

Unlike :mod:`urio.driver_iocp` (a dedicated poller thread that bounces
completions to the loop via ``call_soon_threadsafe``), this driver reaps
completions **on the event-loop thread**, with no extra thread and no
cross-thread wakeup — the Windows analogue of the Linux eventfd design.

asyncio's ``ProactorEventLoop`` already owns an I/O completion port and blocks
on it in ``IocpProactor._poll``. We open files with overlapped I/O, associate
them with *that* port, and submit reads/writes (with offsets, which the stock
``_overlapped`` module cannot express) through the native ``ProactorIo`` object.
Each submission is registered with the proactor via a tiny overlapped *shim*, so
when the kernel posts the completion the loop wakes itself and resolves the
matching future on the loop thread.
"""

from __future__ import annotations

import asyncio
import weakref

from . import _uring

# Cached as an attribute on the loop object so the driver dies with the loop —
# a WeakKeyDictionary keyed by loop would leak (the driver strongly references
# the loop, keeping the key alive). The dict is only a weak-value fallback for
# loops that reject attribute assignment.
_DRIVER_ATTR = '_urio_proactor_driver'
_drivers: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class _Ov:
    """The minimal overlapped interface ``IocpProactor`` requires: an
    ``address`` (of the native OVERLAPPED), a ``pending`` flag, and ``cancel()``.
    Backed by a Rust-owned op kept alive until its completion is reaped."""

    __slots__ = ('address', 'pending', '_io')

    def __init__(self, address: int, io) -> None:
        self.address = address
        self.pending = True
        self._io = io

    def cancel(self) -> None:
        # File operations run to completion regardless; there is nothing to
        # cancel. Cleanup of an abandoned (cancelled-future) op happens in
        # __del__, by which point the proactor has already reaped the
        # completion, so the kernel is finished with the buffer.
        pass

    def __del__(self) -> None:
        try:
            self._io.discard(self.address)
        except Exception:
            pass


class ProactorDriver:
    """Bridges urio's file ops onto a running ``ProactorEventLoop``'s IOCP."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._proactor = loop._proactor  # type: ignore[attr-defined]
        self._io = _uring.ProactorIo(self._proactor._iocp)

    def _complete(self, transferred, key, ov):
        # Called by IocpProactor._poll on the loop thread; its return value
        # becomes the future's result (or it raises OSError).
        return self._io.take(ov.address, transferred or 0)

    def _await_op(self, address: int, handle: int):
        ov = _Ov(address, self._io)
        return self._proactor._register(ov, handle, self._complete)

    async def open(
        self, path: str, write: bool, create: bool, truncate: bool, exclusive: bool
    ) -> int:
        # CreateFileW is not an overlapped operation; it completes synchronously.
        return self._io.open(path, write, create, truncate, exclusive)

    async def read(self, handle: int, offset: int, length: int) -> bytes:
        address = self._io.submit_read(handle, offset, length)
        data = await self._await_op(address, handle)
        return data if data is not None else b''

    async def write(self, handle: int, offset: int, data: bytes) -> int:
        address = self._io.submit_write(handle, offset, data)
        return await self._await_op(address, handle)

    async def close(self, handle: int) -> None:
        self._io.close(handle)


def get_proactor_driver(
    loop: asyncio.AbstractEventLoop | None = None,
) -> ProactorDriver:
    if loop is None:
        loop = asyncio.get_running_loop()
    driver = getattr(loop, _DRIVER_ATTR, None)
    if driver is None:
        ref = _drivers.get(loop)
        driver = ref() if ref is not None else None
    if driver is None:
        driver = ProactorDriver(loop)
        try:
            setattr(loop, _DRIVER_ATTR, driver)
        except AttributeError:
            _drivers[loop] = weakref.ref(driver)
    return driver


def close_driver(loop: asyncio.AbstractEventLoop) -> None:
    """Discard the driver cached on ``loop``, if any (the proactor's IOCP is
    owned by the loop itself; there is nothing else to tear down)."""
    driver = getattr(loop, _DRIVER_ATTR, None)
    if driver is not None:
        try:
            delattr(loop, _DRIVER_ATTR)
        except AttributeError:
            pass
    else:
        _drivers.pop(loop, None)
