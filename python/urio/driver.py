"""asyncio integration for the native io_uring ring.

A :class:`Driver` owns one :class:`urio._uring.Ring` and bridges its completions
into the running event loop. The ring registers an eventfd with io_uring; we hand
that fd to ``loop.add_reader`` so the loop wakes whenever completions are ready.
On wake we drain the eventfd, reap every completion, and resolve the matching
future. This keeps everything on the loop thread -- no executor, no extra thread,
genuinely async I/O.

One driver is created lazily per event loop and cached, so a ring is shared by
every coroutine running on that loop.
"""

from __future__ import annotations

import asyncio
import collections
import errno as _errno
import os
import weakref
from typing import Any

from . import _uring

_DRIVER_ATTR = '_urio_uring_driver'
_drivers: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

_RETRYABLE = (_errno.EAGAIN, _errno.EBUSY)
_MAX_FLUSH_RETRIES = 16

_DEFAULT_ENTRIES = 512


def _ring_entries() -> int:
    raw = os.environ.get('URIO_RING_ENTRIES')
    if not raw:
        return _DEFAULT_ENTRIES
    try:
        n = int(raw)
    except ValueError:
        return _DEFAULT_ENTRIES
    return min(n, 32768) if n > 0 else _DEFAULT_ENTRIES


def _close_abandoned_open(fut: asyncio.Future) -> None:
    try:
        fd, _ = fut.result()
    except BaseException:
        return
    try:
        os.close(fd)
    except OSError:
        pass


class _Chain:
    """Bookkeeping for one linked open→I/O pair on a fixed-file slot.

    Tracks how many of the two completions are still outstanding so an
    abandoned chain (cancelled or failed awaiter) can close whatever landed in
    the slot and recycle it only once the kernel is done with both ops.
    """

    __slots__ = ('slot', 'open_id', 'open_ok', 'remaining', 'abandoned')

    def __init__(self, slot: int, open_id: int):
        self.slot = slot
        self.open_id = open_id
        self.open_ok = False
        self.remaining = 2
        self.abandoned = False


class Driver:
    """Bridges one io_uring ring into a single asyncio event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, entries: int | None = None):
        if entries is None:
            entries = _ring_entries()
        self._loop = loop
        self._ring = _uring.Ring(entries)
        self._eventfd = self._ring.eventfd
        self._futures: dict[int, asyncio.Future] = {}
        self._closed = False
        self._depth = entries
        self._inflight = 0
        self._waiters: collections.deque[asyncio.Future] = collections.deque()
        self._flush_scheduled = False
        self._unflushed: list[int] = []
        self._flush_retries = 0
        self._untracked: set[int] = set()
        self._open_ids: set[int] = set()
        self._slots: collections.deque[int] = collections.deque(
            range(getattr(self._ring, 'fixed_slots', 0))
        )
        self._chains: dict[int, _Chain] = {}
        self._recycle_on_done: dict[int, int] = {}
        loop.add_reader(self._eventfd, self._on_ready)

    async def _acquire_slot(self) -> None:
        if self._inflight < self._depth:
            self._inflight += 1
            return
        await self._park_for_slot()

    async def _park_for_slot(self) -> None:
        fut = self._loop.create_future()
        self._waiters.append(fut)
        try:
            await fut
        except BaseException:
            if fut.cancelled():
                try:
                    self._waiters.remove(fut)
                except ValueError:
                    pass
            else:
                self._release_slot()
            raise

    def _release_slot(self) -> None:
        while self._waiters:
            w = self._waiters.popleft()
            if not w.done():
                w.set_result(None)
                return
        self._inflight -= 1

    def _schedule_flush(self) -> None:
        if not self._flush_scheduled:
            self._flush_scheduled = True
            self._loop.call_soon(self._flush)

    def _flush(self) -> None:
        self._flush_scheduled = False
        if self._closed:
            return
        try:
            self._ring.submit()
        except OSError as e:
            if e.errno in _RETRYABLE and self._flush_retries < _MAX_FLUSH_RETRIES:
                self._flush_retries += 1
                self._schedule_flush()
                return
            failed, self._unflushed = self._unflushed, []
            self._flush_retries = 0
            for ident in failed:
                fut = self._futures.pop(ident, None)
                self._open_ids.discard(ident)
                self._recycle_on_done.pop(ident, None)
                self._release_slot()
                if fut is not None and not fut.done():
                    fut.set_exception(
                        OSError(e.errno or 0, f'io_uring submit failed: {e}')
                    )
            return
        self._unflushed.clear()
        self._flush_retries = 0
        self._reap()

    def _on_ready(self) -> None:
        """Reader callback: drain the eventfd and resolve completed futures."""
        self._ring.drain_eventfd()
        self._reap()

    def _reap(self) -> None:
        for ident, result, payload in self._ring.reap():
            if ident in self._untracked:
                self._untracked.discard(ident)
                slot = self._recycle_on_done.pop(ident, None)
                if slot is not None and result >= 0:
                    self._slots.append(slot)
                continue
            slot = self._recycle_on_done.pop(ident, None)
            if slot is not None and result >= 0:
                self._slots.append(slot)
            was_open = ident in self._open_ids
            self._open_ids.discard(ident)
            chain = self._chains.pop(ident, None)
            if chain is not None:
                chain.remaining -= 1
                if ident == chain.open_id:
                    chain.open_ok = result >= 0
                if chain.abandoned and chain.remaining == 0:
                    self._finish_chain(chain)
            self._release_slot()
            fut = self._futures.pop(ident, None)
            if fut is None or fut.done():
                if was_open and result >= 0:
                    try:
                        os.close(result)
                    except OSError:
                        pass
                continue
            try:
                if result < 0:
                    fut.set_exception(OSError(-result, os.strerror(-result)))
                else:
                    fut.set_result((result, payload))
            except Exception:
                if not fut.done():
                    fut.cancel()

    def _await_id(self, ident: int) -> asyncio.Future:
        fut = self._loop.create_future()
        self._futures[ident] = fut
        self._unflushed.append(ident)
        return fut

    def _cancel_op(self, ident: int) -> None:
        if self._closed or ident not in self._futures:
            return
        try:
            cid = self._ring.submit_cancel(ident)
        except (OSError, AttributeError):
            return
        self._untracked.add(cid)
        self._schedule_flush()

    async def _run(
        self, submit, *, cancel_kernel: bool = True, opens_fd: bool = False
    ) -> tuple[int, Any]:
        """Submit via ``submit`` (returns an id) and await its completion."""
        if self._closed:
            raise RuntimeError('driver is closed')
        await self._acquire_slot()
        try:
            ident = submit(self._ring)
        except BaseException:
            self._release_slot()
            raise
        self._schedule_flush()
        fut = self._await_id(ident)
        try:
            return await asyncio.shield(fut) if opens_fd else await fut
        except asyncio.CancelledError:
            if opens_fd:
                if fut.done():
                    _close_abandoned_open(fut)
                else:
                    fut.add_done_callback(_close_abandoned_open)
            if cancel_kernel:
                self._cancel_op(ident)
            raise

    async def read(self, fd: int, offset: int, length: int) -> bytes:
        if self._closed:
            raise RuntimeError('driver is closed')
        if self._inflight < self._depth:
            self._inflight += 1
        else:
            await self._park_for_slot()
        try:
            ident = self._ring.submit_read(fd, offset, length)
        except BaseException:
            self._release_slot()
            raise
        self._schedule_flush()
        try:
            _, payload = await self._await_id(ident)
        except asyncio.CancelledError:
            self._cancel_op(ident)
            raise
        return payload if payload is not None else b''

    async def write(
        self, fd: int, offset: int, data: bytes, append: bool = False
    ) -> int:
        off = (1 << 64) - 1 if append else offset
        if self._closed:
            raise RuntimeError('driver is closed')
        if self._inflight < self._depth:
            self._inflight += 1
        else:
            await self._park_for_slot()
        try:
            ident = self._ring.submit_write(fd, off, data)
        except BaseException:
            self._release_slot()
            raise
        self._schedule_flush()
        try:
            result, _ = await self._await_id(ident)
        except asyncio.CancelledError:
            self._cancel_op(ident)
            raise
        return result

    async def writev(
        self, fd: int, offset: int, bufs: list[bytes], append: bool = False
    ) -> int:
        off = (1 << 64) - 1 if append else offset
        result, _ = await self._run(lambda r: r.submit_writev(fd, off, bufs))
        return result

    async def fsync(self, fd: int, datasync: bool = False) -> None:
        await self._run(lambda r: r.submit_fsync(fd, datasync))

    async def ftruncate(self, fd: int, length: int) -> None:
        await self._run(lambda r: r.submit_ftruncate(fd, length))

    async def open(self, path: str, flags: int, mode: int) -> int:
        def submit(r):
            ident = r.submit_open(path, flags, mode)
            self._open_ids.add(ident)
            return ident

        result, _ = await self._run(submit, opens_fd=True)
        return result

    async def close(self, fd: int) -> None:
        await self._run(lambda r: r.submit_close(fd))

    async def statx(self, path: str, follow: bool = True) -> tuple:
        _, payload = await self._run(lambda r: r.submit_statx(path, follow))
        return payload

    async def fstatx(self, fd: int) -> tuple:
        _, payload = await self._run(lambda r: r.submit_fstatx(fd))
        return payload

    async def mkdir(self, path: str, mode: int = 0o777) -> None:
        await self._run(lambda r: r.submit_mkdir(path, mode))

    async def unlink(self, path: str, remove_dir: bool = False) -> None:
        await self._run(lambda r: r.submit_unlink(path, remove_dir))

    async def rename(self, src: str, dst: str) -> None:
        await self._run(lambda r: r.submit_rename(src, dst))

    async def symlink(self, target: str, linkpath: str) -> None:
        await self._run(lambda r: r.submit_symlink(target, linkpath))

    async def link(self, src: str, dst: str) -> None:
        await self._run(lambda r: r.submit_link(src, dst))

    def _finish_chain(self, chain: _Chain) -> None:
        if chain.open_ok:
            self.close_fixed_nowait(chain.slot)
        else:
            self._slots.append(chain.slot)

    def _abandon_chain(self, chain: _Chain, *futs: asyncio.Future) -> None:
        chain.abandoned = True

        def _swallow(f: asyncio.Future) -> None:
            if not f.cancelled():
                f.exception()

        for f in futs:
            if f.done():
                _swallow(f)
            else:
                f.add_done_callback(_swallow)
        if chain.remaining == 0:
            self._finish_chain(chain)

    async def _open_chain(self, submit):
        """Common half of open_read/open_write: allocate a slot, take two
        in-flight slots (two completions), submit the linked pair, and await
        both halves with abandoned-chain cleanup. Returns (slot, second-op
        payload/result) or None when no fixed slot is available."""
        if self._closed:
            raise RuntimeError('driver is closed')
        if not self._slots:
            return None
        slot = self._slots.popleft()
        acquired = 0
        try:
            for _ in range(2):
                if self._inflight < self._depth:
                    self._inflight += 1
                else:
                    await self._park_for_slot()
                acquired += 1
            open_id, io_id = submit(self._ring, slot)
        except BaseException:
            for _ in range(acquired):
                self._release_slot()
            self._slots.append(slot)
            raise
        self._schedule_flush()
        ofut = self._await_id(open_id)
        iofut = self._await_id(io_id)
        chain = _Chain(slot, open_id)
        self._chains[open_id] = chain
        self._chains[io_id] = chain
        try:
            await ofut
        except asyncio.CancelledError:
            self._abandon_chain(chain, ofut, iofut)
            raise
        except OSError:
            self._abandon_chain(chain, iofut)
            raise
        try:
            result, payload = await iofut
        except asyncio.CancelledError:
            self._abandon_chain(chain, iofut)
            raise
        except OSError:
            self._abandon_chain(chain)
            raise
        return slot, (payload if payload is not None else result)

    async def open_read(
        self, path: str, flags: int, mode: int, length: int
    ) -> tuple[int, bytes] | None:
        """Linked open+read: one submission opens ``path`` into a fixed slot
        and reads the first ``length`` bytes. Returns ``(slot, data)`` — the
        caller owns the slot and must release it via ``close_fixed`` /
        ``close_fixed_nowait`` — or None when no slot is free."""
        res = await self._open_chain(
            lambda r, s: r.submit_open_read(path, flags, mode, length, s)
        )
        if res is None:
            return None
        slot, payload = res
        return slot, (payload if isinstance(payload, bytes) else b'')

    async def open_write(
        self, path: str, flags: int, mode: int, data: bytes
    ) -> tuple[int, int] | None:
        """Linked open+write of ``data`` at offset 0. Returns
        ``(slot, bytes_written)`` (caller owns the slot) or None."""
        res = await self._open_chain(
            lambda r, s: r.submit_open_write(path, flags, mode, data, s)
        )
        if res is None:
            return None
        return res

    async def read_fixed(self, slot: int, offset: int, length: int) -> bytes:
        _, payload = await self._run(
            lambda r: r.submit_read_fixed(slot, offset, length)
        )
        return payload if payload is not None else b''

    async def write_fixed(self, slot: int, offset: int, data: bytes) -> int:
        result, _ = await self._run(lambda r: r.submit_write_fixed(slot, offset, data))
        return result

    async def close_fixed(self, slot: int) -> None:
        """Recycle the slot only after a successful close completion."""

        def submit(r):
            ident = r.submit_close_fixed(slot)
            self._recycle_on_done[ident] = slot
            return ident

        await self._run(submit, cancel_kernel=False)

    def close_fixed_nowait(self, slot: int) -> None:
        """Fire-and-forget close of ``slot``. The slot returns to the pool only
        when the close completes, so a new chain can never race the old fd."""
        if self._closed:
            return
        try:
            cid = self._ring.submit_close_fixed(slot)
        except OSError:
            return
        self._untracked.add(cid)
        self._recycle_on_done[cid] = slot
        self._schedule_flush()

    @property
    def inflight(self) -> int:
        return self._ring.inflight

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._loop.remove_reader(self._eventfd)
        except Exception:
            pass
        for fut in self._futures.values():
            if not fut.done():
                fut.cancel()
        self._futures.clear()
        self._unflushed.clear()
        self._untracked.clear()
        self._open_ids.clear()
        self._chains.clear()
        self._recycle_on_done.clear()
        while self._waiters:
            w = self._waiters.popleft()
            if not w.done():
                w.cancel()


def get_driver(loop: asyncio.AbstractEventLoop | None = None) -> Driver:
    """Return the driver for ``loop`` (default: the running loop), creating it
    on first use and caching it for the lifetime of the loop."""
    if loop is None:
        loop = asyncio.get_running_loop()
    driver = getattr(loop, _DRIVER_ATTR, None)
    if driver is None:
        ref = _drivers.get(loop)
        driver = ref() if ref is not None else None
    if driver is None:
        driver = Driver(loop)
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
